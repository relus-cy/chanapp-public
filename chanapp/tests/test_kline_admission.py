"""准入：硬拒绝、数值有效性、零成交标注与跨周期核对（spec §5.2、§5.3）。"""
import math
import unittest
from dataclasses import replace

from chanapp.engine.kline import admission as adm
from chanapp.engine.kline.rows import RawDayRow, RawMinuteRow

TODAY = "2026-09-28"


def day(date="2026-09-24", o=10.0, h=11.0, l=9.5, c=10.5, sf=0, pc=10.0, prov="final", v=100.0):
    return RawDayRow("sh600036", date, o, h, l, c, v, "lot", 1000.0, "CNY", pc, sf, prov, "b1")


def minute(slot="2026-09-24 09:35", o=10.0, h=10.2, l=9.9, c=10.1, v=10.0, state="closed",
           trade_state="traded"):
    return RawMinuteRow("sh600036", slot[:10], slot, o, h, l, c, v, "lot", 1.0, state, trade_state, "b1")


class DayAdmissionTests(unittest.TestCase):
    def reasons(self, verdict):
        return [why for _, why in verdict.rejected]

    def test_valid_rows_accepted(self):
        verdict = adm.check_day_rows([day(), day("2026-09-25")], today=TODAY)
        self.assertEqual(len(verdict.accepted), 2)

    def test_duplicate_dates_both_rejected(self):
        verdict = adm.check_day_rows([day(), day()], today=TODAY)
        self.assertEqual(self.reasons(verdict), [adm.DUPLICATE_KEY, adm.DUPLICATE_KEY])

    def test_bad_ohlc_and_non_finite_and_future(self):
        rows = [day(l=10.6), day("2026-09-23", h=math.nan), day("2026-09-30"),
                day("2026-09-22", o=-1.0), day("2026-09-21", v=-5.0)]
        self.assertEqual(self.reasons(adm.check_day_rows(rows, today=TODAY)),
                         [adm.BAD_OHLC, adm.NON_FINITE, adm.OUT_OF_RANGE,
                          adm.NON_POSITIVE_PRICE, adm.NEGATIVE_QUANTITY])

    def test_requested_range_enforced(self):
        # 麦蕊 14 位参数曾越界返回（spec §5.2）：越出请求范围的行拒绝
        verdict = adm.check_day_rows([day("2024-12-01")], today=TODAY,
                                     start="2024-12-10", end="2024-12-20")
        self.assertEqual(self.reasons(verdict), [adm.OUT_OF_RANGE])

    def test_preopen_row_needs_pc_but_not_prices(self):
        ok = day(TODAY, o=None, h=None, l=None, c=None, v=None, prov="preopen", pc=16.77)
        bad = day(TODAY, o=None, h=None, l=None, c=None, v=None, prov="preopen", pc=None)
        self.assertEqual(len(adm.check_day_rows([ok], today=TODAY).accepted), 1)
        self.assertEqual(self.reasons(adm.check_day_rows([bad], today=TODAY)),
                         [adm.PREOPEN_MISSING_PC])

    def test_preopen_must_be_today(self):
        row = day("2026-09-25", o=None, h=None, l=None, c=None, v=None, prov="preopen")
        self.assertEqual(self.reasons(adm.check_day_rows([row], today=TODAY)), [adm.OUT_OF_RANGE])

    def test_suspended_row_may_omit_prices(self):
        row = day(o=None, h=None, l=None, c=None, v=None, sf=1)
        self.assertEqual(len(adm.check_day_rows([row], today=TODAY).accepted), 1)


class MinuteAdmissionTests(unittest.TestCase):
    def test_off_grid_and_lunch_label_rejected(self):
        rows = [minute("2026-09-24 13:00"), minute("2026-09-24 09:30"), minute("2026-09-24 13:41")]
        verdict = adm.check_minute_rows(rows, market="CN", fact_freq="m5", today=TODAY)
        self.assertEqual([why for _, why in verdict.rejected], [adm.OFF_GRID] * 3)

    def test_forming_only_today(self):
        verdict = adm.check_minute_rows([minute(state="forming")], market="CN",
                                        fact_freq="m5", today=TODAY)
        self.assertEqual([why for _, why in verdict.rejected], [adm.FORMING_NOT_TODAY])

    def test_placeholder_marked_no_trade(self):
        rows = [minute("2024-09-27 10:25", 34.96, 35.0, 34.96, 34.97, 1212.0),
                minute("2024-09-27 10:30", 34.97, 34.97, 34.97, 34.97, 0.0),
                minute("2024-09-27 11:25", 34.99, 34.99, 34.99, 34.99, 759.0)]
        marked = adm.mark_trade_state(rows, prev_traded_close=None)
        self.assertEqual([r.trade_state for r in marked], ["traded", "no_trade", "traded"])

    def test_placeholder_uses_prev_traded_close_across_batches(self):
        row = minute("2024-09-27 10:30", 34.97, 34.97, 34.97, 34.97, 0.0)
        self.assertEqual(adm.mark_trade_state([row], prev_traded_close=34.97)[0].trade_state, "no_trade")
        self.assertEqual(adm.mark_trade_state([row], prev_traded_close=35.10)[0].trade_state, "traded")


class CrossCheckTests(unittest.TestCase):
    def test_minute_day_mismatch(self):
        minutes = [minute("2026-09-24 09:35", 10.0, 10.2, 9.9, 10.1),
                   minute("2026-09-24 15:00", 10.1, 10.6, 10.0, 10.5)]
        good = {"open": 10.0, "high": 10.6, "low": 9.9, "close": 10.5, "sf": 0}
        self.assertIsNone(adm.minute_day_mismatch(minutes, good, tol=1e-6))
        bad = dict(good, low=9.5)
        self.assertEqual(adm.minute_day_mismatch(minutes, bad, tol=1e-6), adm.MINUTE_DAY_OHLC)

    def test_zero_volume_bars_ignored_in_ohlc(self):
        minutes = [minute("2024-09-27 10:30", 34.97, 34.97, 34.97, 34.97, 0.0),
                   minute("2024-09-27 11:25", 34.99, 34.99, 34.99, 34.99, 759.0)]
        self.assertEqual(adm.traded_ohlc(minutes), (34.99, 34.99, 34.99, 34.99))

    def test_volume_conservation_is_soft(self):
        minutes = [minute(v=10.0), minute("2026-09-24 15:00", v=10.0)]
        self.assertIsNone(adm.volume_not_conserved(minutes, {"volume": 20.0, "volume_unit": "lot"}))
        self.assertEqual(adm.volume_not_conserved(minutes, {"volume": 25.0, "volume_unit": "lot"}),
                         adm.VOLUME_NOT_CONSERVED)
