"""周线（spec §6.2）。"""
import unittest

from chanapp.engine.kline import periods


def d(dt, o, h, l, c, v=100, forming=False):
    return {"dt": dt, "open": o, "high": h, "low": l, "close": c, "volume": v, "amount": 1.0,
            "forming": forming}


TD = ["2024-12-30", "2024-12-31", "2025-01-02", "2025-01-03",
      "2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10"]


class WeeklyTests(unittest.TestCase):
    def test_cross_year_week_not_split_and_labelled_by_last_trading_day(self):
        days = [d("2024-12-30", 10, 11, 9, 10.5), d("2025-01-03", 10.5, 12, 10, 11.5)]
        bars, incomplete = periods.week_bars(days, trading_days=TD, suspended=set(),
                                             today="2025-01-10")
        self.assertEqual(len(bars), 1)
        self.assertEqual((bars[0]["dt"], bars[0]["week_start"]), ("2025-01-03", "2024-12-30"))
        self.assertEqual((bars[0]["open"], bars[0]["high"], bars[0]["low"], bars[0]["close"]),
                         (10, 12, 9, 11.5))
        self.assertEqual(incomplete, [{"week": "2024-12-30", "missing": ["2024-12-31", "2025-01-02"]}])

    def test_suspension_is_not_missing(self):
        days = [d("2024-12-30", 10, 11, 9, 10.5), d("2025-01-03", 10.5, 12, 10, 11.5)]
        _, incomplete = periods.week_bars(days, trading_days=TD,
                                          suspended={"2024-12-31", "2025-01-02"}, today="2025-01-10")
        self.assertEqual(incomplete, [])

    def test_fully_suspended_week_has_no_bar(self):
        bars, _ = periods.week_bars([], trading_days=TD, suspended=set(TD[4:]), today="2025-01-10")
        self.assertEqual(bars, [])

    def test_current_week_forming(self):
        days = [d("2025-01-06", 1, 1, 1, 1), d("2025-01-07", 1, 2, 1, 2)]
        bars, _ = periods.week_bars(days, trading_days=TD, suspended=set(), today="2025-01-07")
        self.assertTrue(bars[-1]["forming"])

    def test_unknown_calendar_marks_incomplete(self):
        bars, incomplete = periods.week_bars([d("2031-01-06", 1, 1, 1, 1)], trading_days=TD,
                                             suspended=set(), today="2031-01-07")
        self.assertEqual(incomplete, [{"week": "2031-01-06", "reason": "unknown_calendar"}])

    def test_week_source_follows_last_day(self):
        days = [dict(d("2025-01-06", 1, 1, 1, 1), source="pytdx", sources=["pytdx"]),
                dict(d("2025-01-07", 1, 2, 1, 2), source="mairui", sources=["mairui"])]
        bars, _ = periods.week_bars(days, trading_days=TD, suspended=set(), today="2025-01-10")
        self.assertEqual((bars[0]["source"], bars[0]["sources"]), ("mairui", ["mairui", "pytdx"]))
