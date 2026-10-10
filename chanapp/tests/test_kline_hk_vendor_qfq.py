"""港股供应商前复权缓存（spec §7 方案 (b) 落地规则 2、3、6）与视图接线。"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from chanapp.engine.kline import facts, hk_vendor_qfq as vq, views
from chanapp.engine.kline.rows import RawDayRow, RawMinuteRow, new_batch_id

CODE = "hk00700"
NOW = datetime(2026, 9, 28, 20, 0)          # 周一收盘后
TODAY = "2026-09-28"
DAYS = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"]


def vday(d, close):
    return {"trade_date": d, "open": close, "high": close + 1, "low": close - 1, "close": close,
            "volume": 1000.0, "amount": None, "volume_unit": "share"}


def vm30(slot, close):
    return {"trade_date": slot[:10], "slot_end": slot, "open": close, "high": close, "low": close,
            "close": close, "volume": 100.0, "amount": None, "volume_unit": "share"}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = facts.open_facts(Path(tmp.name) / facts.DB_NAME)
        self.addCleanup(self.conn.close)

    def raw_days(self, days, close=100.0):
        facts.commit_day_rows(self.conn, [RawDayRow(CODE, d, close, close + 1, close - 1, close, 1000, "share",
                                                    None, "HKD", close, 0, "final", new_batch_id())
                                          for d in days],
                              market="HK", kind="stock", item="day_history", source="longbridge",
                              binding_gen=1, today=TODAY)
        with facts.write_txn(self.conn):
            for d in days:
                self.conn.execute("INSERT OR REPLACE INTO calendar(market, date, is_open, sessions, source,"
                                  " fetched_at) VALUES ('HK', ?, 1, '[]', 'test', ?)", (d, facts.now_iso()))


class CacheTests(Base):
    def test_publish_is_versioned_and_keeps_old_versions(self):
        v1 = vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS], closed_through=DAYS[-1])
        v2 = vq.publish(self.conn, CODE, "day", [vday(d, 51.0) for d in DAYS], closed_through=DAYS[-1])
        self.assertEqual((v1, v2), (1, 2))
        bars, meta = vq.read(self.conn, CODE, "day")
        self.assertEqual({b["close"] for b in bars}, {51.0})
        self.assertEqual(meta["version"], 2)
        kept = self.conn.execute("SELECT COUNT(*) FROM vendor_qfq_bars WHERE code=? AND version=1",
                                 (CODE,)).fetchone()[0]
        self.assertEqual(kept, 5)

    def test_publish_drops_bars_of_today_and_later(self):
        vq.publish(self.conn, CODE, "m30", [vm30("2026-09-25 16:00", 1.0), vm30(f"{TODAY} 10:00", 2.0)],
                   closed_through=DAYS[-1])
        bars, _ = vq.read(self.conn, CODE, "m30")
        self.assertEqual([b["dt"] for b in bars], ["2026-09-25 16:00"])

    def test_needs_refetch_only_on_closed_overlap_differences(self):
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS], closed_through=DAYS[-1])
        same = [vday(d, 50.0) for d in DAYS]
        self.assertFalse(vq.needs_refetch(self.conn, CODE, "day", same + [vday(TODAY, 99.0)], closed_through=DAYS[-1]))
        self.assertFalse(vq.needs_refetch(self.conn, CODE, "day", same + [vday("2026-09-29", 1.0)],
                                          closed_through=DAYS[-1]))
        restated = [vday(d, 49.0) for d in DAYS]
        self.assertTrue(vq.needs_refetch(self.conn, CODE, "day", restated, closed_through=DAYS[-1]))

    def test_fresh_window_not_touching_cache_end_needs_refetch(self):
        # 缓存超过比对窗口没刷新：新窗口与缓存无重叠时不能直接追加（会留断档、混用复权基准）
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS[:2]], closed_through=DAYS[-1])
        later = [vday("2026-09-24", 50.0), vday("2026-09-25", 50.0)]
        self.assertTrue(vq.needs_refetch(self.conn, CODE, "day", later, closed_through=DAYS[-1]))

    def test_empty_series_is_not_published(self):
        with self.assertRaises(ValueError):
            vq.publish(self.conn, CODE, "day", [], closed_through=DAYS[-1])

    def test_old_versions_pruned_beyond_retention(self):
        for close in range(8):
            vq.publish(self.conn, CODE, "day", [vday(d, 50.0 + close) for d in DAYS], closed_through=DAYS[-1])
        versions = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT version FROM vendor_qfq_bars WHERE code=? ORDER BY version", (CODE,))]
        self.assertEqual(versions, [4, 5, 6, 7, 8])

    def test_vendor_m60_pagination_with_before(self):
        self.raw_days(DAYS)
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS], closed_through=DAYS[-1])
        vq.publish(self.conn, CODE, "m30", [vm30(f"{d} {t}", 1.0) for d in DAYS[-2:]
                                            for t in ("10:00", "10:30", "11:00", "11:30")],
                   closed_through=DAYS[-1])
        page = views.read_view(self.conn, CODE, "m60", adjust="qfq", before="2026-09-25 00:00", limit=1, now=NOW)
        self.assertEqual([b["dt"][:10] for b in page.bars], ["2026-09-24"])
        self.assertTrue(page.has_more)

    def test_frozen_cache_rejects_publish_and_reads_as_stale(self):
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS], closed_through=DAYS[-1])
        vq.freeze(self.conn, CODE)
        with self.assertRaises(vq.VendorFrozen):
            vq.publish(self.conn, CODE, "day", [vday(d, 51.0) for d in DAYS], closed_through=DAYS[-1])
        _, meta = vq.read(self.conn, CODE, "day")
        self.assertTrue(meta["frozen"])

    def test_mark_stale_keeps_published_version(self):
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS], closed_through=DAYS[-1])
        vq.mark_stale(self.conn, CODE, "day")
        bars, meta = vq.read(self.conn, CODE, "day")
        self.assertEqual((len(bars), meta["version"], meta["stale"]), (5, 1, True))

    def test_quarantined_raw_dates_are_not_served(self):
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS], closed_through=DAYS[-1])
        vq.publish(self.conn, CODE, "m30", [vm30(f"{d} 16:00", 50.0) for d in DAYS], closed_through=DAYS[-1])
        with facts.write_txn(self.conn):
            facts.quarantine(self.conn, CODE, "day", "2026-09-23", "proven_wrong")
            facts.quarantine(self.conn, CODE, "m30", "2026-09-24 16:00", "proven_wrong")
        days = [b["dt"] for b in vq.read(self.conn, CODE, "day")[0]]
        slots = [b["dt"] for b in vq.read(self.conn, CODE, "m30")[0]]
        self.assertNotIn("2026-09-23", days)
        self.assertNotIn("2026-09-24 16:00", slots)

    def test_invalid_vendor_series_is_rejected_and_keeps_current_version(self):
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS], closed_through=DAYS[-1])
        bad_high = dict(vday(DAYS[2], 50.0), high=49.0)
        negative = vday(DAYS[2], -1.0)
        nan = dict(vday(DAYS[2], 50.0), close=float("nan"))
        neg_volume = dict(vday(DAYS[2], 50.0), volume=-5.0)
        for bad in (bad_high, negative, nan, neg_volume):
            series = [vday(d, 49.0) for d in DAYS]
            series[2] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                vq.publish(self.conn, CODE, "day", series, closed_through=DAYS[-1])
        dup = [vday(d, 49.0) for d in DAYS] + [vday(DAYS[0], 49.0)]
        with self.assertRaises(ValueError):
            vq.publish(self.conn, CODE, "day", dup, closed_through=DAYS[-1])
        bars, meta = vq.read(self.conn, CODE, "day")
        self.assertEqual((meta["version"], {b["close"] for b in bars}), (1, {50.0}))

    def test_extend_appends_new_closed_bars_as_new_version(self):
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS[:3]], closed_through=DAYS[-1])
        version = vq.extend(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS], closed_through=DAYS[-1])
        bars, meta = vq.read(self.conn, CODE, "day")
        self.assertEqual((version, meta["version"], len(bars)), (2, 2, 5))


class PublishSetTests(Base):
    def series(self, close, days=DAYS):
        return {"day": [vday(d, close) for d in days], "m30": [vm30(f"{d} 16:00", close) for d in days]}

    def test_publishes_both_periods_in_one_step(self):
        out = vq.publish_set(self.conn, CODE, self.series(50.0), closed_through=DAYS[-1], full=True)
        self.assertEqual(out, {"day": 1, "m30": 1})

    def test_invalid_period_leaves_both_unchanged(self):
        vq.publish_set(self.conn, CODE, self.series(50.0), closed_through=DAYS[-1], full=True)
        bad = self.series(49.0)
        bad["m30"][2] = dict(bad["m30"][2], high=1.0)
        with self.assertRaises(ValueError):
            vq.publish_set(self.conn, CODE, bad, closed_through=DAYS[-1], full=True)
        self.assertEqual([m[1] for m in vq.cache_version(self.conn, CODE)], [1, 1])
        self.assertEqual({b["close"] for b in vq.read(self.conn, CODE, "day")[0]}, {50.0})

    def test_changed_binding_gen_rejects_publish(self):
        from chanapp.engine.kline import bindings
        from chanapp.engine.kline.rows import FetchItem
        gens = {"day_history": 1, "minute_history": 1}
        bindings.switch(self.conn, "HK", "stock", FetchItem.DAY_HISTORY, "longbridge", reason="drill")
        with self.assertRaises(facts.StaleBinding):
            vq.publish_set(self.conn, CODE, self.series(50.0), closed_through=DAYS[-1], full=True,
                           binding_gens=gens)
        self.assertEqual(vq.cache_version(self.conn, CODE), [])

    def test_frozen_cache_published_only_with_unfreeze(self):
        vq.publish_set(self.conn, CODE, self.series(50.0), closed_through=DAYS[-1], full=True)
        vq.freeze(self.conn, CODE)
        with self.assertRaises(vq.VendorFrozen):
            vq.publish_set(self.conn, CODE, self.series(49.0), closed_through=DAYS[-1], full=True)
        vq.publish_set(self.conn, CODE, self.series(49.0), closed_through=DAYS[-1], full=True, unfreeze=True)
        self.assertEqual(vq.cache_version(self.conn, CODE), [["day", 2, 0, 0], ["m30", 2, 0, 0]])

    def test_extend_mode_appends_and_clears_stale_without_new_version(self):
        vq.publish_set(self.conn, CODE, self.series(50.0, DAYS[:3]), closed_through=DAYS[2], full=True)
        vq.mark_stale(self.conn, CODE)
        fresh = self.series(50.0)
        fresh["m30"] = fresh["m30"][:3]                     # m30 没有新收盘 bar
        out = vq.publish_set(self.conn, CODE, fresh, closed_through=DAYS[-1], full=False)
        self.assertEqual(out, {"day": 2, "m30": None})
        self.assertEqual(vq.cache_version(self.conn, CODE), [["day", 2, 0, 0], ["m30", 1, 0, 0]])

    def test_late_candidate_after_newer_publish_is_rejected(self):
        # 同一绑定代次：取数前记下的缓存版本在发布时已变（期间别的刷新先发布了更新版本），迟到候选整组丢弃
        with self.assertRaises(vq.VendorSuperseded):
            vq.publish_set(self.conn, CODE, self.series(50.0), closed_through=DAYS[-1], full=True,
                           expected_versions={"day": 1, "m30": 1})
        self.assertEqual(vq.cache_version(self.conn, CODE), [])
        vq.publish_set(self.conn, CODE, self.series(51.0), closed_through=DAYS[-1], full=True,
                       expected_versions={"day": None, "m30": None})               # 首建：取数前没有缓存
        with self.assertRaises(vq.VendorSuperseded):
            vq.publish_set(self.conn, CODE, self.series(50.0), closed_through=DAYS[-1], full=True,
                           expected_versions={"day": None, "m30": None})
        self.assertEqual(vq.cache_version(self.conn, CODE), [["day", 1, 0, 0], ["m30", 1, 0, 0]])
        self.assertEqual({b["close"] for b in vq.read(self.conn, CODE, "m30")[0]}, {51.0})

    def test_extend_mode_with_one_period_empty_publishes_neither(self):
        # 追加模式下一个周期取回为空：与全量模式一致视为不合法，两个都不发布、不清 stale
        vq.publish_set(self.conn, CODE, self.series(50.0, DAYS[:3]), closed_through=DAYS[2], full=True)
        vq.mark_stale(self.conn, CODE)
        fresh = self.series(50.0)
        fresh["m30"] = []
        with self.assertRaises(ValueError):
            vq.publish_set(self.conn, CODE, fresh, closed_through=DAYS[-1], full=False)
        self.assertEqual(vq.cache_version(self.conn, CODE), [["day", 1, 0, 1], ["m30", 1, 0, 1]])

    def test_extend_mode_validates_overlap(self):
        # 重叠区（缓存末端之前）的 NaN 或重复标签不会进入追加结果，但说明整段候选不可信
        vq.publish_set(self.conn, CODE, self.series(50.0, DAYS[:3]), closed_through=DAYS[2], full=True)
        nan = self.series(50.0)
        nan["day"][1] = dict(nan["day"][1], close=float("nan"))
        dup = self.series(50.0)
        dup["m30"].append(vm30(f"{DAYS[0]} 16:00", 50.0))
        for bad in (nan, dup):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                vq.publish_set(self.conn, CODE, bad, closed_through=DAYS[-1], full=False)
        self.assertEqual(vq.cache_version(self.conn, CODE), [["day", 1, 0, 0], ["m30", 1, 0, 0]])


class VendorViewTests(Base):
    def test_hk_qfq_day_reads_cache_with_vendor_label(self):
        self.raw_days(DAYS, close=100.0)
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS], closed_through=DAYS[-1])
        view = views.read_view(self.conn, CODE, "day", adjust="qfq", now=NOW)
        self.assertEqual([b["close"] for b in view.bars], [50.0] * 5)
        self.assertEqual(view.adjust_label, "前复权（供应商口径）")
        raw = views.read_view(self.conn, CODE, "day", adjust="raw", now=NOW)
        self.assertEqual([b["close"] for b in raw.bars], [100.0] * 5)

    def live_today(self, close):
        facts.commit_day_rows(self.conn, [RawDayRow(CODE, TODAY, close, close, close, close, 10, "share",
                                                    None, "HKD", 100.0, 0, "live", new_batch_id())],
                              market="HK", kind="stock", item="day_history", source="longbridge",
                              binding_gen=1, today=TODAY)

    def test_fresh_cache_gets_only_todays_live_bar_appended(self):
        self.raw_days(DAYS, close=100.0)
        self.live_today(101.0)
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS], closed_through=DAYS[-1])
        view = views.read_view(self.conn, CODE, "day", adjust="qfq", now=datetime(2026, 9, 28, 10, 0))
        self.assertEqual([b["close"] for b in view.bars], [50.0] * 5 + [101.0])
        self.assertTrue(view.bars[-1]["forming"])

    def test_cache_lagging_closed_days_serves_cache_only_and_is_stale(self):
        # 缓存停在 09-24，raw 已有 09-25 收盘：再接 raw 会跨越可能的除净日混用复权基准
        self.raw_days(DAYS, close=100.0)
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS[:4]], closed_through=DAYS[3])
        view = views.read_view(self.conn, CODE, "day", adjust="qfq", now=NOW)
        self.assertEqual([b["close"] for b in view.bars], [50.0] * 4)
        self.assertTrue(view.stale)

    def test_stale_or_frozen_cache_never_splices_raw(self):
        self.raw_days(DAYS, close=100.0)
        self.live_today(101.0)
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS], closed_through=DAYS[-1])
        vq.mark_stale(self.conn, CODE, "day")
        stale = views.read_view(self.conn, CODE, "day", adjust="qfq", now=datetime(2026, 9, 28, 10, 0))
        self.assertEqual([b["close"] for b in stale.bars], [50.0] * 5)
        self.assertTrue(stale.stale)
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS], closed_through=DAYS[-1])
        vq.freeze(self.conn, CODE)
        frozen = views.read_view(self.conn, CODE, "day", adjust="qfq", now=datetime(2026, 9, 28, 10, 0))
        self.assertEqual(len(frozen.bars), 5)
        self.assertTrue(frozen.stale)

    def test_m60_aggregates_cached_m30_and_week_aggregates_cached_day(self):
        self.raw_days(DAYS)
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS], closed_through=DAYS[-1])
        vq.publish(self.conn, CODE, "m30", [vm30("2026-09-25 10:00", 1.0), vm30("2026-09-25 10:30", 2.0)],
                   closed_through=DAYS[-1])
        m60 = views.read_view(self.conn, CODE, "m60", adjust="qfq", now=NOW)
        self.assertEqual([(b["dt"], b["open"], b["close"], b["volume"]) for b in m60.bars],
                         [("2026-09-25 10:30", 1.0, 2.0, 200.0)])
        week = views.read_view(self.conn, CODE, "week", adjust="qfq", now=NOW)
        self.assertEqual([(b["dt"], b["close"]) for b in week.bars], [("2026-09-25", 50.0)])

    def test_republish_changes_token(self):
        self.raw_days(DAYS)
        vq.publish(self.conn, CODE, "day", [vday(d, 50.0) for d in DAYS], closed_through=DAYS[-1])
        before = views.read_view(self.conn, CODE, "day", adjust="qfq", now=NOW).token
        vq.publish(self.conn, CODE, "day", [vday(d, 49.0) for d in DAYS], closed_through=DAYS[-1])
        self.assertNotEqual(views.read_view(self.conn, CODE, "day", adjust="qfq", now=NOW).token, before)

    def test_no_cache_yet_is_empty_not_raw_and_not_unsupported(self):
        # 缓存未建是「支持但数据暂缺」：空视图（门面据此同步首取），不回落 raw，也不标「该市场暂不提供」
        self.raw_days(DAYS)
        for freq in ("day", "week", "m60", "m30"):
            view = views.read_view(self.conn, CODE, freq, adjust="qfq", now=NOW)
            self.assertEqual(view.bars, [], freq)
            self.assertNotIn("unsupported", [n["code"] for n in view.notices], freq)
        m5 = views.read_view(self.conn, CODE, "m5", adjust="qfq", now=NOW)
        self.assertEqual([n["code"] for n in m5.notices], ["unsupported"])   # 细周期才是确实不支持


if __name__ == "__main__":
    unittest.main()
