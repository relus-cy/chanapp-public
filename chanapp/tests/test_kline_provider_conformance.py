"""raw provider 一致性：每个 provider 用录制 fixture 跑同一套 RawRow 断言（spec §5.2、§10；计划 A4）。

断言：分钟 slot_end 在市场网格上且为末端标签、(trade_date, slot_end) 唯一、volume_unit 合法、
日线有 pc/sf 而分钟没有、数值一律有限、state/provenance 合法；每个模块定义 CONTRACT_VERSION；
新模块不 import 旧 backend、factors、adjust（计划 A 决定 11）。
"""
import dataclasses
import json
import math
import re
import unittest
from datetime import datetime
from pathlib import Path

from chanapp.engine.kline import sessions
from chanapp.engine.kline.providers import baostock_raw, longbridge, mairui, pytdx_raw, yahoo_raw
from chanapp.engine.kline.rows import (BAR_STATES, PROVENANCE_RANK, SHARES_PER_UNIT, TRADE_STATES,
                                       RawDayRow, RawMinuteRow, market_of)

FIX = Path(__file__).resolve().parent / "fixtures" / "kline_raw"
NEW_MODULES = (baostock_raw, pytdx_raw, yahoo_raw, longbridge)


def _load(*parts):
    return json.loads(FIX.joinpath(*parts).read_text())


def _mairui(name):
    rec = _load("mairui", name)
    return mairui.MairuiProvider(transport=lambda *a: (rec["status"], rec["body"]), licence="x")


def _tdx(name):
    rows = _load("pytdx", name)["rows"]
    return pytdx_raw.PytdxRawProvider(query=lambda kind, cat, market, num, offset, count:
                                      list(rows) if offset == 0 else [])


def _baostock(name):
    rec = _load("baostock", name)
    return baostock_raw.BaostockRawProvider(query=lambda fn, **kw: (rec["fields"], rec["rows"]))


def _yahoo(name):
    payload = _load("yahoo", name)
    return yahoo_raw.YahooRawProvider(fetch=lambda sym, params: payload)


class _LB:
    def __init__(self):
        self.data = {(p, a): _load("longbridge", f"{p}-{a}-700.json")["rows"]
                     for p in ("day", "m30") for a in ("raw", "qfq")}
        self.live = _load("longbridge", "candlesticks-live-700.json")["rows"]

    def history_by_date(self, symbol, period, adjust, start, end):
        return [r for r in self.data[(period, adjust)] if start <= r["timestamp"][:10] <= end]

    def candlesticks(self, symbol, period, count, adjust):
        return self.live[-count:]


LATER = datetime(2026, 12, 31, 20, 0)

# (名称, 取数闭包, 期望行类型, 分钟周期或 None)
CASES = [
    ("mairui day", lambda: _mairui("day-300209-2024q4.json").day_history("sz300209", "2024-10-01", "2025-01-31"),
     RawDayRow, None),
    ("mairui m5", lambda: _mairui("m5-600036-20240927.json").minute_history(
        "sh600036", "m5", "2024-09-27 09:30", "2024-09-27 15:00", now=LATER), RawMinuteRow, "m5"),
    ("baostock day", lambda: _baostock("day-600036-202409.json").day_history("sh600036", "2024-09-01", "2024-09-30"),
     RawDayRow, None),
    ("baostock day suspended", lambda: _baostock("day-688981-suspended.json").day_history(
        "sh688981", "2025-08-27", "2025-09-05"), RawDayRow, None),
    ("pytdx index day", lambda: _tdx("day-sh000001-202609.json").day_history("sh000001", "2026-09-01", "2026-09-11"),
     RawDayRow, None),
    ("pytdx index m5", lambda: _tdx("m5-sh000001-20260811.json").minute_history(
        "sh000001", "m5", "2026-08-11 09:30", "2026-08-11 15:00", now=LATER), RawMinuteRow, "m5"),
    ("pytdx stock m5", lambda: _tdx("m5-sz300209-20260811.json").minute_history(
        "sz300209", "m5", "2026-08-11 09:30", "2026-08-11 15:00", now=LATER), RawMinuteRow, "m5"),
    ("pytdx handwritten m5", lambda: _tdx("m5-handwritten.json").minute_history(
        "sz300209", "m5", "2026-09-14 09:30", "2026-09-14 15:00", now=LATER), RawMinuteRow, "m5"),
    ("yahoo day", lambda: _yahoo("chart-06166-1d.json").day_history("hk06166", "2026-09-08", "2026-09-22"),
     RawDayRow, None),
    ("yahoo m30", lambda: _yahoo("chart-06166-30m.json").minute_history(
        "hk06166", "m30", "2026-09-21 09:30", "2026-09-22 16:00", now=LATER), RawMinuteRow, "m30"),
    ("longbridge day", lambda: longbridge.LongbridgeProvider(client=_LB()).day_history(
        "hk00700", "2026-05-08", "2026-05-22"), RawDayRow, None),
    ("longbridge m30", lambda: longbridge.LongbridgeProvider(client=_LB()).minute_history(
        "hk00700", "m30", "2026-05-14 09:30", "2026-05-15 16:00", now=LATER), RawMinuteRow, "m30"),
    ("longbridge live", lambda: longbridge.LongbridgeProvider(client=_LB()).minute_live(
        "hk00700", "m30", now=datetime(2026, 5, 15, 11, 40)), RawMinuteRow, "m30"),
]

_NUMERIC = ("open", "high", "low", "close", "volume", "amount", "pc")


class ConformanceTests(unittest.TestCase):
    def test_rows_conform(self):
        for name, fetch, row_type, freq in CASES:
            with self.subTest(name):
                rows = fetch()
                self.assertTrue(rows, "fixture 应产生行")
                self.assertTrue(all(type(r) is row_type for r in rows))
                for r in rows:
                    self.assertIn(r.volume_unit, SHARES_PER_UNIT)
                    for field in _NUMERIC:
                        value = getattr(r, field, None)
                        if value is not None:
                            self.assertIsInstance(value, (int, float), f"{field}={value!r}")
                            self.assertTrue(math.isfinite(value), f"{field}={value!r}")
                    self.assertTrue(r.batch_id)
                if row_type is RawDayRow:
                    self.assertTrue(all(r.sf in (0, 1) and isinstance(r.sf, int) for r in rows))
                    self.assertTrue(all(r.provenance in PROVENANCE_RANK for r in rows))
                    self.assertTrue(all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.trade_date) for r in rows))
                    self.assertEqual(len({r.trade_date for r in rows}), len(rows))
                    self.assertLessEqual({"pc", "sf"}, {f.name for f in dataclasses.fields(rows[0])})
                else:
                    market = market_of(rows[0].code)
                    grid = sessions.slots(market, freq)
                    for r in rows:
                        self.assertEqual(r.slot_end[:10], r.trade_date)
                        self.assertIn(r.slot_end[11:], grid, r.slot_end)
                        self.assertIn(r.state, BAR_STATES)
                        self.assertIn(r.trade_state, TRADE_STATES)
                        self.assertFalse(hasattr(r, "pc") or hasattr(r, "sf"))
                    keys = [(r.trade_date, r.slot_end) for r in rows]
                    self.assertEqual(len(set(keys)), len(keys), "slot_end 重复")
                    self.assertEqual(keys, sorted(keys), "行按时间升序")

    def test_every_provider_defines_contract_version(self):
        for module in NEW_MODULES + (mairui,):
            with self.subTest(module.__name__):
                version = module.CONTRACT_VERSION
                self.assertRegex(version, r"^[a-z]+-raw-\d+$")
                provider_cls = next(v for v in vars(module).values()
                                    if isinstance(v, type) and v.__module__ == module.__name__
                                    and getattr(v, "CONTRACT_VERSION", None) == version and hasattr(v, "name"))
                self.assertTrue(provider_cls.name)
        self.assertEqual({m.CONTRACT_VERSION for m in NEW_MODULES},
                         {"baostock-raw-1", "pytdx-raw-1", "yahoo-raw-1", "longbridge-raw-3"})

    def test_new_modules_do_not_import_legacy_helpers(self):
        # 旧 backend 与旧内核（D8）已删除；按源码文本断言，不依赖被删文件是否存在
        banned = re.compile(r"^\s*(from|import)\s+[^\n]*\b(\w+_backend|factors|adjust|factstore|derive|registry"
                            r"|kline\.(?:api|base))\b", re.M)
        for module in NEW_MODULES + (mairui,):
            with self.subTest(module.__name__):
                source = Path(module.__file__).read_text()
                self.assertIsNone(banned.search(source), banned.search(source) and banned.search(source).group(0))
                self.assertNotIn("importlib", source)

    def test_fixtures_carry_no_credentials(self):
        pattern = re.compile(r"(?i)(licence|app_key|app_secret|access_token|authorization|bearer)")
        for source in ("baostock", "pytdx", "yahoo", "longbridge"):
            for path in (FIX / source).glob("*.json"):
                with self.subTest(str(path.name)):
                    self.assertIsNone(pattern.search(path.read_text()))


if __name__ == "__main__":
    unittest.main()
