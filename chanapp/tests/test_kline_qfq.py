"""等比前复权因子链（spec §6.1）。"""
import unittest

from chanapp.engine.kline import qfq

DAYS = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28"]


def row(date, close, pc, sf=0, prov="final"):
    return {"trade_date": date, "close": close, "pc": pc, "sf": sf, "provenance": prov}


class ChainTests(unittest.TestCase):
    def test_no_events_all_ones(self):
        rows = [row("2026-09-21", 10, 9.9), row("2026-09-22", 10.5, 10), row("2026-09-23", 10.2, 10.5)]
        chain = qfq.build_chain(rows, DAYS, today="2026-09-23")
        self.assertEqual(chain.anchor, "2026-09-23")
        self.assertEqual(chain.qfq_from, "2026-09-21")
        self.assertEqual(set(chain.multipliers.values()), {1.0})

    def test_exdate_factor_applies_before_exdate_only(self):
        # 09-23 除权：pc 5.0，前收 10.0 → 之前价格 ×0.5；除权日当天不乘
        rows = [row("2026-09-21", 10, 9.9), row("2026-09-22", 10, 10), row("2026-09-23", 5.1, 5.0)]
        chain = qfq.build_chain(rows, DAYS, today="2026-09-23")
        self.assertEqual(chain.multipliers["2026-09-23"], 1.0)
        self.assertAlmostEqual(chain.multipliers["2026-09-22"], 0.5)
        self.assertAlmostEqual(chain.multipliers["2026-09-21"], 0.5)

    def test_multiple_events_multiply(self):
        rows = [row("2026-09-21", 10, 9.9), row("2026-09-22", 8, 5.0), row("2026-09-23", 4, 4.0)]
        chain = qfq.build_chain(rows, DAYS, today="2026-09-23")
        self.assertAlmostEqual(chain.multipliers["2026-09-21"], 0.5 * 0.5)

    def test_between_two_events_only_later_factor_applies(self):
        # 迁自旧复权测试「多事件递推」：两事件之间只乘后一事件的因子，最后一个除权日及之后不乘
        rows = [row("2026-09-21", 10, 9.9), row("2026-09-22", 8, 5.0), row("2026-09-23", 4, 4.0)]
        chain = qfq.build_chain(rows, DAYS, today="2026-09-23")
        self.assertAlmostEqual(chain.multipliers["2026-09-22"], 0.5)
        self.assertEqual(chain.multipliers["2026-09-23"], 1.0)

    def test_exdate_during_suspension_uses_resume_day_pc(self):
        # 300209 型：停牌期间占位价与 pc 不可信，复牌日 pc 对停牌前最后收盘
        rows = [row("2026-09-21", 10, 9.9), row("2026-09-22", 10, 7.0, sf=1),
                row("2026-09-23", 10, 7.0, sf=1), row("2026-09-24", 5.2, 5.0)]
        chain = qfq.build_chain(rows, DAYS, today="2026-09-24")
        self.assertAlmostEqual(chain.multipliers["2026-09-21"], 0.5)
        self.assertNotIn("2026-09-22", chain.multipliers)   # 停牌日不是价格 bar

    def test_gap_stops_coverage(self):
        rows = [row("2026-09-21", 10, 9.9), row("2026-09-23", 10, 10), row("2026-09-24", 10, 10)]
        chain = qfq.build_chain(rows, DAYS, today="2026-09-24")
        self.assertEqual((chain.qfq_from, chain.stop_reason), ("2026-09-23", "gap"))
        self.assertNotIn("2026-09-21", chain.multipliers)

    def test_missing_pc_never_defaults_to_one(self):
        rows = [row("2026-09-21", 10, 9.9), row("2026-09-22", 10, None), row("2026-09-23", 10, 10)]
        chain = qfq.build_chain(rows, DAYS, today="2026-09-23")
        self.assertEqual((chain.qfq_from, chain.stop_reason), ("2026-09-22", "missing_pc"))

    def test_unknown_calendar_stops(self):
        rows = [row("2026-09-18", 10, 9.9), row("2026-09-21", 10, 10)]
        chain = qfq.build_chain(rows, DAYS, today="2026-09-21")
        self.assertEqual((chain.qfq_from, chain.stop_reason), ("2026-09-21", "unknown_calendar"))

    def test_unknown_weekday_between_rows_stops_instead_of_linking(self):
        # 09-22 既不在开市日也不在已知休市日：无法区分休市与日历缺失，不得跨越计算因子
        rows = [row("2026-09-21", 10, 9.9), row("2026-09-23", 5.1, 5.0)]
        days = [d for d in DAYS if d != "2026-09-22"]
        chain = qfq.build_chain(rows, days, today="2026-09-23")
        self.assertEqual((chain.qfq_from, chain.stop_reason), ("2026-09-23", "unknown_calendar"))
        known = qfq.build_chain(rows, days, closed_days={"2026-09-22"}, today="2026-09-23")
        self.assertEqual((known.qfq_from, known.stop_reason), ("2026-09-21", None))
        self.assertAlmostEqual(known.multipliers["2026-09-21"], 0.5)

    def test_today_confirmed_by_preopen_row(self):
        rows = [row("2026-09-25", 20.0, 19.8),
                row("2026-09-28", None, 16.77, prov="preopen")]
        chain = qfq.build_chain(rows, DAYS, today="2026-09-28")
        self.assertTrue(chain.today_confirmed)
        self.assertEqual(chain.anchor, "2026-09-28")
        self.assertAlmostEqual(chain.multipliers["2026-09-25"], 16.77 / 20.0)
        self.assertEqual(chain.multipliers["2026-09-28"], 1.0)

    def test_today_unconfirmed_anchor_stays_on_last_confirmed_day(self):
        rows = [row("2026-09-24", 20.0, 19.8), row("2026-09-25", 20.0, 20.0)]
        chain = qfq.build_chain(rows, DAYS, today="2026-09-28")
        self.assertFalse(chain.today_confirmed)
        self.assertEqual(chain.anchor, "2026-09-25")
        self.assertNotIn("2026-09-28", chain.multipliers)

    def test_bad_factor_stops(self):
        rows = [row("2026-09-21", 1e-320, 9.9), row("2026-09-22", 10, 10)]
        chain = qfq.build_chain(rows, DAYS, today="2026-09-22")
        self.assertEqual(chain.stop_reason, "bad_factor")
