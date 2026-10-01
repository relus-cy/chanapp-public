"""长桥港股 provider：m30 末端标签、收市竞价并入 16:00、pc、qfq_series、lazy import（spec §5.2、§7）。

录制 fixture 驱动（700.HK，2026-05-15 除净日前后），不联网、不读凭据、不导入 SDK。
"""
import json
import os
import subprocess
import sys
import unittest
import unittest.mock
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from chanapp.engine.kline.providers import longbridge, raw

FIX = Path(__file__).resolve().parent / "fixtures" / "kline_raw" / "longbridge"


def _rows(name):
    return json.loads((FIX / name).read_text())["rows"]


class FakeClient:
    def __init__(self, by_date=None, live=None):
        self.by_date, self.live, self.calls = by_date or {}, live, []

    def history_by_date(self, symbol, period, adjust, start, end):
        self.calls.append(("history_by_date", symbol, period, adjust, start, end))
        return [r for r in self.by_date.get((period, adjust), []) if start <= r["timestamp"][:10] <= end]

    def candlesticks(self, symbol, period, count, adjust):
        self.calls.append(("candlesticks", symbol, period, count, adjust))
        return self.live[-count:]


def provider(**kw):
    client = FakeClient(by_date={("day", "raw"): _rows("day-raw-700.json"),
                                 ("day", "qfq"): _rows("day-qfq-700.json"),
                                 ("m30", "raw"): _rows("m30-raw-700.json"),
                                 ("m30", "qfq"): _rows("m30-qfq-700.json")},
                        live=_rows("candlesticks-live-700.json"), **kw)
    return longbridge.LongbridgeProvider(client=client), client


class LongbridgeTests(unittest.TestCase):
    NOW = datetime(2026, 5, 18, 9, 0)

    def m30(self):
        p, c = provider()
        return p.minute_history("hk00700", "m30", "2026-05-14 09:30", "2026-05-15 16:00", now=self.NOW), c

    def test_symbol_mapping(self):
        self.assertEqual(longbridge._symbol("hk00700"), "700.HK")
        self.assertEqual(longbridge._symbol("hk06166"), "6166.HK")

    def test_start_labels_shift_to_end_labels(self):
        rows, c = self.m30()
        self.assertEqual(c.calls[0][:4], ("history_by_date", "700.HK", "m30", "raw"))
        day = [r.slot_end[11:] for r in rows if r.trade_date == "2026-05-14"]
        self.assertEqual(day, ["10:00", "10:30", "11:00", "11:30", "12:00", "13:30",
                               "14:00", "14:30", "15:00", "15:30", "16:00"])

    def test_closing_auction_merged_into_1600(self):
        rows, _ = self.m30()
        src = {r["timestamp"]: r for r in _rows("m30-raw-700.json")}
        reg, auc = src["2026-05-14T15:30:00"], src["2026-05-14T16:00:00"]
        at_1600 = [r for r in rows if r.slot_end == "2026-05-14 16:00"]
        self.assertEqual(len(at_1600), 1)
        bar = at_1600[0]
        self.assertEqual(bar.volume, float(reg["volume"]) + float(auc["volume"]))
        self.assertAlmostEqual(bar.amount, float(reg["turnover"]) + float(auc["turnover"]), places=3)
        self.assertEqual(bar.open, float(reg["open"]))
        self.assertEqual(bar.close, float(auc["close"]))
        self.assertEqual(bar.high, max(float(reg["high"]), float(auc["high"])))
        self.assertEqual(bar.low, min(float(reg["low"]), float(auc["low"])))

    def test_eleven_m30_bars_per_day_and_volume_conserved(self):
        rows, _ = self.m30()
        self.assertEqual(Counter(r.trade_date for r in rows), {"2026-05-14": 11, "2026-05-15": 11})
        day = {r["timestamp"][:10]: float(r["volume"]) for r in _rows("day-raw-700.json")}
        for d in ("2026-05-14", "2026-05-15"):
            self.assertEqual(sum(r.volume for r in rows if r.trade_date == d), day[d])
        self.assertEqual({(r.state, r.volume_unit) for r in rows}, {("closed", "share")})

    def test_day_rows_raw_with_previous_close(self):
        p, c = provider()
        rows = p.day_history("hk00700", "2026-05-11", "2026-05-22")
        self.assertEqual(c.calls[0][3], "raw")
        self.assertEqual(rows[0].trade_date, "2026-05-11")
        self.assertIsNotNone(rows[0].pc)            # 回看窗口给首行前收
        for prev, cur in zip(rows, rows[1:]):
            self.assertEqual(cur.pc, prev.close)
        self.assertEqual({(r.currency, r.volume_unit, r.sf, r.provenance) for r in rows},
                         {("HKD", "share", 0, "final")})
        self.assertIsInstance(rows[0].open, float)

    def test_auction_price_outside_continuous_range_widens_high_low(self):
        # V2 实测 1888.HK 2023-06-13：开 7.48 > 高 7.47（开市前竞价成交不计入上游高低），独立来源同值
        def row(day, o, h, l, c):
            return {"timestamp": f"{day}T00:00:00", "open": o, "high": h, "low": l, "close": c,
                    "volume": 1818077, "turnover": "13443433.000"}
        rows = [row("2023-06-12", "7.380", "7.480", "7.260", "7.430"),
                row("2023-06-13", "7.480", "7.470", "7.260", "7.460"),      # 开盘高于最高 0.13%
                row("2023-06-14", "7.260", "7.310", "7.300", "7.110"),      # 收盘低于最低 2.6%：不补
                row("2023-06-15", "7.110", "7.380", "7.120", "7.350")]      # 开盘低于最低 0.14%
        p = longbridge.LongbridgeProvider(client=FakeClient(by_date={("day", "raw"): rows,
                                                                     ("day", "qfq"): rows}))
        with self.assertLogs(longbridge.log, "WARNING"):
            got = {r.trade_date: r for r in p.day_history("hk01888", "2023-06-12", "2023-06-15")}
        self.assertEqual((got["2023-06-13"].high, got["2023-06-13"].low), (7.48, 7.26))
        self.assertEqual((got["2023-06-15"].high, got["2023-06-15"].low), (7.38, 7.11))
        self.assertEqual((got["2023-06-14"].high, got["2023-06-14"].low), (7.31, 7.30))   # 留给准入拒收
        qfq = {b["trade_date"]: b for b in p.qfq_series("hk01888", "day", "2023-06-12", "2023-06-15")}
        self.assertEqual(qfq["2023-06-13"]["high"], 7.48)

    def test_qfq_series_uses_same_label_rules(self):
        p, c = provider()
        rows = p.qfq_series("hk00700", "m30", "2026-05-14 09:30", "2026-05-15 16:00")
        self.assertEqual(c.calls[0][3], "qfq")
        raw_rows, _ = self.m30()
        self.assertEqual([r["slot_end"] for r in rows], [r.slot_end for r in raw_rows])
        self.assertEqual(Counter(r["trade_date"] for r in rows), {"2026-05-14": 11, "2026-05-15": 11})
        src = {r["timestamp"]: r for r in _rows("m30-qfq-700.json")}
        bar = next(r for r in rows if r["slot_end"] == "2026-05-14 16:00")
        self.assertEqual(bar["volume"], float(src["2026-05-14T15:30:00"]["volume"])
                         + float(src["2026-05-14T16:00:00"]["volume"]))
        day = p.qfq_series("hk00700", "day", "2026-05-11", "2026-05-22")
        self.assertEqual(day[0]["trade_date"], "2026-05-11")
        self.assertNotEqual(day[0]["close"], p.day_history("hk00700", "2026-05-11", "2026-05-22")[0].close)

    def test_minute_live_today_only_forming(self):
        p, c = provider()
        rows = p.minute_live("hk00700", "m30", now=datetime(2026, 5, 15, 11, 40))
        self.assertEqual(c.calls[0], ("candlesticks", "700.HK", "m30", 12, "raw"))
        self.assertEqual([r.slot_end[11:] for r in rows], ["10:00", "10:30", "11:00", "11:30", "12:00"])
        self.assertEqual({(r.state, r.trade_date) for r in rows}, {("forming", "2026-05-15")})

    def test_off_session_start_label_dropped_not_fatal(self):
        live = _rows("candlesticks-live-700.json")
        pre = dict(live[-1], timestamp="2026-05-15T09:00:00")      # 盘前起始标签：不在会话内
        client = FakeClient(live=live[:-1] + [pre] + live[-1:])
        with self.assertLogs("chanapp.engine.kline.providers.longbridge", "WARNING"):
            rows = longbridge.LongbridgeProvider(client=client).minute_live(
                "hk00700", "m30", now=datetime(2026, 5, 15, 11, 40))
        self.assertEqual([r.slot_end[11:] for r in rows], ["10:00", "10:30", "11:00", "11:30", "12:00"])

    def test_m5_not_verified_yet(self):
        p, c = provider()
        with self.assertRaisesRegex(raw.ProviderUnsupported, "m5 待 PH1"):
            p.minute_history("hk00700", "m5", "2026-05-14 09:30", "2026-05-15 16:00", now=self.NOW)
        with self.assertRaisesRegex(raw.ProviderUnsupported, "m5 待 PH1"):
            p.minute_live("hk00700", "m5", now=self.NOW)
        self.assertEqual(c.calls, [])

    def test_client_error_is_provider_error(self):
        class Broken:
            def history_by_date(self, *a):
                raise ConnectionError("upstream reset")
        with self.assertRaises(raw.ProviderError):
            longbridge.LongbridgeProvider(client=Broken()).day_history("hk00700", "2026-05-11", "2026-05-22")

    def test_sdk_not_imported(self):
        self.assertNotIn("longbridge", sys.modules)
        # 独立进程再证一次：导入 provider 并构造实例（不取数）不会拉起 SDK
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        for name in ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN"):
            env.pop(name, None)
        code = ("import sys; from chanapp.engine.kline.providers import longbridge as m; "
                "m.LongbridgeProvider(); print('longbridge' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "False")

    def test_naive_sdk_time_is_host_local_not_hkt(self):
        # V2 实测：SDK 返回主机本地时区的 naive 时间；主机 TZ=UTC 时港股 00:00 日线变成前一天 16:00
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        code = ("from datetime import datetime; from chanapp.engine.kline.providers import longbridge as m; "
                "print(m._plain(datetime(2026, 9, 13, 16, 0)), m._plain(datetime(2026, 9, 14, 1, 30)))")
        for tz, want in (("UTC", "2026-09-14T00:00:00 2026-09-14T09:30:00"),
                         ("Asia/Shanghai", "2026-09-13T16:00:00 2026-09-14T01:30:00")):
            out = subprocess.run([sys.executable, "-c", code], env=dict(env, TZ=tz), capture_output=True,
                                 text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), want, tz)


if __name__ == "__main__":
    unittest.main()


class LongbridgeErrorClassTests(unittest.TestCase):
    def test_network_failure_is_connection_class(self):
        class Broken:
            def history_by_date(self, *args):
                raise TimeoutError("read timed out")

        p = longbridge.LongbridgeProvider(client=Broken())
        with self.assertRaises(raw.ProviderConnectionError):
            p.day_history("hk00700", "2026-05-14", "2026-05-15")

    def test_missing_credentials_is_connection_class(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            for name in longbridge._CRED_NAMES:
                os.environ.pop(name, None)
            with self.assertRaises(raw.ProviderConnectionError):
                longbridge.SdkClient()._context()


class FakeCalendarClient:
    """按 PH4 录制回答 trading_days；与 SDK 同样拒绝超过 30 天的区间和一年以前的起点。
    录制窗口外的日期视为供应商未公布（返回空）。fail 中的起点整段抛错。"""

    def __init__(self, today, fail=()):
        rec = json.loads((FIX / "trading-days-hk.json").read_text())
        self.lo, self.hi = rec["window"]
        self.days, self.half = set(rec["trading_days"]), set(rec["half_trading_days"])
        self.today, self.fail, self.calls = today, dict(fail), []

    def trading_days(self, begin, end):
        self.calls.append((begin, end))
        b, e = date.fromisoformat(begin), date.fromisoformat(end)
        if (e - b).days > 30:
            raise RuntimeError("OpenApiException: (code=301600) the interval must be less than one month")
        if b < self.today - timedelta(days=366):
            raise RuntimeError("OpenApiException: (code=301600) only the most recent year")
        for start, exc in self.fail.items():
            if begin <= start <= end:
                raise exc
        pick = lambda s: sorted(d for d in s if begin <= d <= end and self.lo <= d <= self.hi)
        return {"trading_days": pick(self.days), "half_trading_days": pick(self.half)}


class LongbridgeCalendarTests(unittest.TestCase):
    """港股交易日历（F5）：长桥 trading_days 分段取年表；半日市带上午会话；未公布与取数失败的日子留作未知。"""
    TODAY = date(2026, 9, 28)

    def calendar(self, year, **kw):
        client = FakeCalendarClient(self.TODAY, **kw)
        p = longbridge.LongbridgeProvider(client=client, today=lambda: self.TODAY)
        return {r.date: r for r in p.calendar(year)}, client

    def test_holidays_half_days_and_open_days(self):
        rows, _ = self.calendar(2026)
        self.assertEqual({r.market for r in rows.values()}, {"HK"})
        self.assertTrue(rows["2026-09-30"].is_open)
        self.assertFalse(rows["2026-10-01"].is_open)            # 国庆休市
        self.assertTrue(rows["2026-10-02"].is_open)
        self.assertFalse(rows["2026-12-25"].is_open)
        for half in ("2026-02-16", "2026-12-24", "2026-12-31"):
            self.assertTrue(rows[half].is_open, half)
            self.assertEqual(rows[half].sessions, (("09:30", "12:00"),), half)
        self.assertEqual(rows["2026-09-30"].sessions, ())
        self.assertNotIn("2026-10-03", rows)                    # 周末不出行（日历读法按周末休市）
        self.assertFalse(rows["2026-01-01"].is_open)

    def test_requests_respect_sdk_window_limits(self):
        _, client = self.calendar(2026)
        spans = [(date.fromisoformat(b), date.fromisoformat(e)) for b, e in client.calls]
        self.assertEqual(spans[0][0], date(2026, 1, 1))
        self.assertEqual(spans[-1][1], date(2026, 12, 31))
        for (b, e), (nb, _) in zip(spans, spans[1:]):
            self.assertEqual(nb, e + timedelta(days=1))
        self.assertTrue(all((e - b).days <= 30 for b, e in spans))
        _, old = self.calendar(2025)                            # 一年以前的段不请求
        self.assertTrue(all(date.fromisoformat(b) >= self.TODAY - timedelta(days=366) for b, _ in old.calls))

    def test_failed_window_leaves_its_days_unknown(self):
        rows, _ = self.calendar(2026, fail={"2026-10-01": RuntimeError("OpenApiException: (code=301600)")})
        self.assertNotIn("2026-10-01", rows)
        self.assertNotIn("2026-09-30", rows)                    # 同一段整段未知
        self.assertFalse(rows["2026-12-25"].is_open)            # 其他段照常

    def test_all_windows_failing_raises(self):
        calls = []

        class Down:
            def trading_days(self, b, e):
                calls.append(b)
                raise TimeoutError("read timed out")
        p = longbridge.LongbridgeProvider(client=Down(), today=lambda: self.TODAY)
        with self.assertRaises(raw.ProviderConnectionError):
            p.calendar(2026)
        self.assertEqual(len(calls), 1, "连接类失败说明整个源不可用，不再请求后面的分段")

    def test_unpublished_horizon_is_unknown_not_closed(self):
        rows, _ = self.calendar(2027)                           # 录制只到 2027-01-26
        self.assertTrue(rows["2027-01-26"].is_open)
        # 注：录制里 2027-01-01 被列为交易日（元旦必休），说明供应商次年假日尚未更新；按供应商原样记录
        self.assertNotIn("2027-01-27", rows)
        self.assertNotIn("2027-06-01", rows)
