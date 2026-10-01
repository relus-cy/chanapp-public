"""冷备保活：只比对不入库；只有取回并比对通过至少一行才算 pass（spec §8、§13）。"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from chanapp.engine.kline import bindings, facts, keepalive
from chanapp.engine.kline.providers.raw import ProviderError
from chanapp.engine.kline.rows import FetchItem, RawDayRow, RawMinuteRow, new_batch_id

NOW = datetime(2026, 9, 26, 10, 0)
DAYS = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"]


def day(code, d, close=10.0, volume=100, unit="lot"):
    return RawDayRow(code, d, close, close + 1, close - 1, close, volume, unit, 1000.0, "CNY", close,
                     0, "final", new_batch_id())


class Cold:
    """冷备：按 mode 返回与主源一致的行、空列表、偏离的行或抛错。"""

    def __init__(self, mode="same"):
        self.mode, self.calls = mode, []

    def day_history(self, code, start, end):
        self.calls.append(("day", code, start, end))
        if self.mode == "raise":
            raise ProviderError("down")
        if self.mode == "empty":
            return []
        close = 10.5 if self.mode == "off" else 10.0
        # 冷备以「股」计量：换算后应与主源的「手」相等
        return [day(code, d, close, volume=10000, unit="share") for d in DAYS if start <= d <= end]

    def minute_history(self, code, fact_freq, start, end, *, now):
        self.calls.append(("minute", code, start, end))
        return []


class KeepaliveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = facts.open_facts(Path(tmp.name) / facts.DB_NAME)
        self.addCleanup(self.conn.close)

    def seed(self):
        facts.commit_day_rows(self.conn, [day("sh600036", d) for d in DAYS], market="CN", kind="stock",
                              item=FetchItem.DAY_HISTORY.value, source="mairui", binding_gen=1,
                              today="2026-09-26")

    def run_with(self, cold):
        providers = {"baostock": cold, "pytdx": Cold("empty"), "yahoo": Cold("empty")}
        return keepalive.run(self.conn, providers.__getitem__, now=NOW)

    def verdict(self):
        return bindings.verdict_of(self.conn, "CN", "stock", FetchItem.DAY_HISTORY, "baostock")

    def test_matching_rows_pass(self):
        self.seed()
        results = self.run_with(Cold("same"))
        self.assertEqual(self.verdict(), "pass")
        row = next(r for r in results if r["probe_id"] == "keepalive-P1")
        self.assertEqual(row["compared"], 5)

    def test_empty_response_fails(self):
        self.seed()
        self.run_with(Cold("empty"))
        self.assertEqual(self.verdict(), "fail")

    def test_divergent_prices_fail(self):
        self.seed()
        self.run_with(Cold("off"))
        self.assertEqual(self.verdict(), "fail")

    def test_error_fails(self):
        self.seed()
        self.run_with(Cold("raise"))
        self.assertEqual(self.verdict(), "fail")

    def test_rows_written_by_the_cold_source_are_not_used_as_reference(self):
        # 手动切到冷备期间入库的行来自冷备：拿它比冷备没有意义，不记录
        facts.commit_day_rows(self.conn, [day("sh600036", d) for d in DAYS], market="CN", kind="stock",
                              item=FetchItem.DAY_HISTORY.value, source="baostock", binding_gen=1,
                              today="2026-09-26")
        cold = Cold("same")
        self.run_with(cold)
        self.assertEqual(cold.calls, [])

    def test_index_prices_compared_within_rounding_tolerance(self):
        from chanapp.engine.kline import keepalive as ka
        primary = {"open": 3000.01, "high": 3010.0, "low": 2990.0, "close": 3005.0, "volume": 1, "volume_unit": "lot"}
        cold = dict(primary, open=3000.0049)      # 麦蕊两位小数 vs 冷备高精度：舍入边界差 0.01
        self.assertTrue(ka._match(primary, cold, "index", "CN"))
        self.assertFalse(ka._match(primary, dict(primary, close=3005.05), "index", "CN"))

    def test_no_primary_rows_records_nothing(self):
        cold = Cold("same")
        self.run_with(cold)
        self.assertEqual(cold.calls, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM probe_runs").fetchone()[0], 0)

    def test_minute_preference_skips_minute_probe_but_keeps_day_probe(self):
        self.seed()
        binding = bindings.binding("CN", "stock", FetchItem.MINUTE_HISTORY)
        facts.commit_minute_rows(
            self.conn, [RawMinuteRow("sh600036", "2026-09-25", "2026-09-25 15:00", 10, 10, 10, 10, 1, "lot",
                                     1, "closed", "traded", new_batch_id())],
            market="CN", kind="stock", item=FetchItem.MINUTE_HISTORY.value,
            fact_freq=binding.minute_fact_freq, source=binding.primary, binding_gen=1, today="2026-09-26")
        cold = Cold("same")
        keepalive.run(self.conn, lambda source: cold, now=NOW, minute_enabled=lambda code: False)
        self.assertTrue([call for call in cold.calls if call[0] == "day"])
        self.assertFalse([call for call in cold.calls if call[0] == "minute"])

    def test_keepalive_never_writes_facts(self):
        self.seed()
        facts.commit_minute_rows(
            self.conn, [RawMinuteRow("sh600036", "2026-09-25", "2026-09-25 15:00", 10, 10, 10, 10, 1, "lot",
                                     1, "closed", "traded", new_batch_id())],
            market="CN", kind="stock", item=FetchItem.MINUTE_HISTORY.value, fact_freq="m5",
            source="mairui", binding_gen=1, today="2026-09-26")
        count = lambda t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        before = (count("day_bars"), count("minute_bars"), count("batches"))
        self.run_with(Cold("same"))
        self.assertEqual((count("day_bars"), count("minute_bars"), count("batches")), before)


if __name__ == "__main__":
    unittest.main()
