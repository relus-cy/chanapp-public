"""周期派生（spec §6.2，执行计划 A 决定 4）。"""
import json
import unittest
from pathlib import Path

from chanapp.engine.kline import periods

FIX = Path(__file__).resolve().parent / "fixtures" / "kline_raw" / "mairui"


def m(slot, o, h, l, c, v, trade_state="traded", state="closed", unit="lot"):
    return {"slot_end": slot, "trade_date": slot[:10], "open": o, "high": h, "low": l, "close": c,
            "volume": v, "volume_unit": unit, "amount": 1.0, "state": state, "trade_state": trade_state}


class AggregateTests(unittest.TestCase):
    def test_cn_m60_buckets_by_clock_not_row_count(self):
        rows = [m("2026-09-24 09:35", 10, 10.1, 9.9, 10, 1), m("2026-09-24 10:30", 10, 10.3, 10, 10.2, 1),
                m("2026-09-24 13:05", 10.2, 10.4, 10.1, 10.3, 1)]
        bars = periods.aggregate_minutes(rows, market="CN", fact_freq="m5", target_freq="m60")
        self.assertEqual([b["dt"] for b in bars], ["2026-09-24 10:30", "2026-09-24 14:00"])
        self.assertEqual(bars[0]["slots"], 2)
        self.assertEqual(bars[0]["volume"], 200)   # 手 → 股

    def test_placeholder_bars_do_not_set_open(self):
        rows = [m("2024-09-27 11:05", 34.97, 34.97, 34.97, 34.97, 0),
                m("2024-09-27 11:25", 34.99, 34.99, 34.99, 34.99, 759),
                m("2024-09-27 11:30", 35.3, 35.36, 34.91, 35.21, 86871)]
        bar = periods.aggregate_minutes(rows, market="CN", fact_freq="m5", target_freq="m30")[0]
        self.assertEqual((bar["open"], bar["high"], bar["low"], bar["close"]), (34.99, 35.36, 34.91, 35.21))

    def test_all_placeholder_bucket_is_flat_zero_volume(self):
        rows = [m(f"2024-09-27 10:{mm}", 34.97, 34.97, 34.97, 34.97, 0) for mm in ("35", "40", "55")]
        bar = periods.aggregate_minutes(rows, market="CN", fact_freq="m5", target_freq="m30")[0]
        self.assertEqual((bar["dt"], bar["open"], bar["close"], bar["volume"]),
                         ("2024-09-27 11:00", 34.97, 34.97, 0))

    def test_suspended_rows_produce_no_bar(self):
        rows = [m("2026-09-24 09:35", 10, 10, 10, 10, 0, trade_state="suspended")]
        self.assertEqual(periods.aggregate_minutes(rows, market="CN", fact_freq="m5",
                                                   target_freq="m30"), [])

    def test_hk_m30_to_m60_short_morning_bucket(self):
        rows = [m("2026-09-24 11:30", 1, 1, 1, 1, 1, unit="share"),
                m("2026-09-24 12:00", 1, 2, 1, 2, 1, unit="share"),
                m("2026-09-24 16:00", 2, 2, 2, 2, 1, unit="share")]
        bars = periods.aggregate_minutes(rows, market="HK", fact_freq="m30", target_freq="m60")
        self.assertEqual([b["dt"][11:] for b in bars], ["11:30", "12:00", "16:00"])

    def test_finer_than_fact_is_unsupported(self):
        with self.assertRaises(periods.UnsupportedPeriod):
            periods.aggregate_minutes([], market="HK", fact_freq="m30", target_freq="m5")

    def test_forming_propagates(self):
        rows = [m("2026-09-28 09:35", 10, 10, 10, 10, 1), m("2026-09-28 09:40", 10, 10, 10, 10, 1, state="forming")]
        self.assertTrue(periods.aggregate_minutes(rows, market="CN", fact_freq="m5",
                                                  target_freq="m30")[0]["forming"])

    def test_today_day_bar(self):
        rows = [m("2026-09-28 09:35", 10, 10.5, 9.8, 10.2, 5), m("2026-09-28 09:40", 10.2, 10.6, 10.1, 10.4, 5)]
        bar = periods.day_bar_from_minutes(rows, trade_date="2026-09-28", forming=True)
        self.assertEqual((bar["dt"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]),
                         ("2026-09-28", 10, 10.6, 9.8, 10.4, 1000))

    @unittest.skipUnless((FIX / "m5-600036-20240927.json").exists(), "fixture from Task A13")
    def test_vendor_m30_reproduced_from_m5(self):
        # P2 复核：600036 2024-09-27 八根 m30 与麦蕊原生逐根一致
        m5 = json.loads((FIX / "m5-600036-20240927.json").read_text())["body"]
        native = json.loads((FIX / "m30-600036-20240927.json").read_text())["body"]
        rows = [m(r["t"][:16], r["o"], r["h"], r["l"], r["c"], r["v"]) for r in m5]
        bars = periods.aggregate_minutes(rows, market="CN", fact_freq="m5", target_freq="m30")
        self.assertEqual([(b["dt"], b["open"], b["high"], b["low"], b["close"], b["volume"] / 100) for b in bars],
                         [(r["t"][:16], r["o"], r["h"], r["l"], r["c"], r["v"]) for r in native])
