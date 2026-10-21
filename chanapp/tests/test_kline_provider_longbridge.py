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
ERR = Path(__file__).resolve().parent / "fixtures" / "kline_errors"   # 不进契约哈希


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
    LATER = datetime(2026, 10, 21, 12, 0)

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

    @staticmethod
    def _opening_auction_client(day_raw, day_qfq):
        # 6127.HK 2024-12-10 生产取证（2026-10-21）：09:30 起的 m30 开 10.30 高于高 10.18 达 1.18%（超出容差），
        # 当日日线开 = 高 = 10.30，持续交易最高只到 10.18：10.30 是开市前竞价成交
        def bar(ts, o, h, l, c, v="613600"):
            return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": v, "turnover": "1.0"}
        return FakeClient(by_date={
            ("m30", "raw"): [bar("2024-12-10T09:30:00", "10.300", "10.180", "9.560", "9.570"),
                             bar("2024-12-10T10:00:00", "9.620", "9.790", "9.590", "9.700")],
            ("m30", "qfq"): [bar("2024-12-10T09:30:00", "10.159", "10.039", "9.419", "9.429"),
                             bar("2024-12-10T10:00:00", "9.479", "9.649", "9.449", "9.559")],
            ("day", "raw"): [bar("2024-12-10T00:00:00", *day_raw, v="1609416")],
            ("day", "qfq"): [bar("2024-12-10T00:00:00", *day_qfq, v="1609416")]})

    def test_opening_auction_beyond_tolerance_widened_when_day_bar_corroborates(self):
        client = self._opening_auction_client(("10.300", "10.300", "9.450", "9.450"),
                                              ("10.159", "10.159", "9.309", "9.309"))
        p = longbridge.LongbridgeProvider(client=client)
        with self.assertLogs(longbridge.log, "WARNING"):
            rows = p.minute_history("hk06127", "m30", "2024-12-10 09:30", "2024-12-10 16:00", now=self.NOW)
        self.assertEqual([(r.slot_end, r.open, r.high, r.low, r.close) for r in rows],
                         [("2024-12-10 10:00", 10.30, 10.30, 9.56, 9.57),
                          ("2024-12-10 10:30", 9.62, 9.79, 9.59, 9.70)])
        qfq = p.qfq_series("hk06127", "m30", "2024-12-10 09:30", "2024-12-10 16:00")
        self.assertEqual([(b["open"], b["high"], b["low"], b["close"]) for b in qfq],
                         [(10.159, 10.159, 9.419, 9.429), (9.479, 9.649, 9.449, 9.559)])

    def test_opening_auction_below_low_widened_by_day_open(self):
        # 6127.HK 2025-03-06：m30 开 14.00 低于低 14.20 达 1.41%，日线开 = 低 = 14.00
        def bar(ts, o, h, l, c):
            return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": "1103900", "turnover": "1.0"}
        client = FakeClient(by_date={("m30", "raw"): [bar("2025-03-06T09:30:00", "14.000", "15.000", "14.200", "14.940")],
                                     ("day", "raw"): [bar("2025-03-06T00:00:00", "14.000", "15.340", "14.000", "14.720")]})
        p = longbridge.LongbridgeProvider(client=client)
        with self.assertLogs(longbridge.log, "WARNING"):
            rows = p.minute_history("hk06127", "m30", "2025-03-06 09:30", "2025-03-06 16:00", now=self.NOW)
        self.assertEqual([(r.open, r.high, r.low, r.close) for r in rows], [(14.0, 15.0, 14.0, 14.94)])

    def test_beyond_tolerance_without_day_corroboration_left_for_admission(self):
        # 日线开盘价不等于越界的 m30 开盘价（或日线高低不含它）：不补，留给准入按 bad_ohlc 拒收
        for day_raw in (("10.200", "10.300", "9.450", "9.450"),     # 开不等
                        ("10.300", "10.000", "9.450", "9.450")):    # 日线高 10.00 不含 10.30（越出 3%，日线自身也不补）
            p = longbridge.LongbridgeProvider(client=self._opening_auction_client(day_raw, day_raw))
            rows = p.minute_history("hk06127", "m30", "2024-12-10 09:30", "2024-12-10 16:00", now=self.NOW)
            self.assertEqual((rows[0].open, rows[0].high, rows[0].low), (10.30, 10.18, 9.56))

    @staticmethod
    def _off_grid_client(drop_m1_bucket=False):
        rec = json.loads((FIX / "m30-offgrid-6127-20261005.json").read_text())["rows"]
        by = {(period, adjust): rec[f"{period}-{adjust}"] for period in ("m30", "m1", "day") for adjust in ("raw", "qfq")}
        if drop_m1_bucket:
            for adjust in ("raw", "qfq"):
                by[("m1", adjust)] = [r for r in by[("m1", adjust)] if not "13:30" <= r["timestamp"][11:16] < "14:00"]
        return FakeClient(by_date=by)

    def test_off_grid_m30_slot_filled_from_m1(self):
        # 6127.HK 2026-10-05 实测：m30 有一根起始标签 13:29（不在半点上），原先被当作会话外丢弃，14:00 槽缺失
        client = self._off_grid_client()
        p = longbridge.LongbridgeProvider(client=client)
        with self.assertLogs(longbridge.log, "WARNING"):
            rows = p.minute_history("hk06127", "m30", "2026-10-05 09:30", "2026-10-05 16:00", now=self.LATER)
        got = {r.slot_end[11:]: (r.open, r.high, r.low, r.close, r.volume) for r in rows}
        self.assertEqual(list(got), ["10:00", "10:30", "11:00", "11:30", "12:00", "13:30",
                                     "14:00", "14:30", "15:00", "15:30", "16:00"])
        self.assertEqual(got["14:00"], (30.52, 30.70, 30.46, 30.66, 37118))     # m1 13:30–13:59 聚合
        self.assertEqual(got["13:30"], (30.46, 30.66, 30.44, 30.46, 66172))     # 半点上的 m30 保留原值
        self.assertEqual(got["16:00"], (30.50, 30.64, 30.42, 30.64, 108722 + 22400))   # 收市竞价照常并入
        self.assertEqual([c[2:4] + c[4:] for c in client.calls if c[2] == "m1"],
                         [("m1", "raw", "2026-10-05", "2026-10-05")])            # 只多取一次当日 m1
        qfq = {b["slot_end"][11:]: (b["open"], b["high"], b["low"], b["close"], b["volume"])
               for b in p.qfq_series("hk06127", "m30", "2026-10-05 09:30", "2026-10-05 16:00")}
        self.assertEqual(qfq["14:00"], (30.52, 30.70, 30.46, 30.66, 37118))
        self.assertEqual(len(qfq), 11)

    def test_off_grid_bucket_without_m1_stays_missing(self):
        p = longbridge.LongbridgeProvider(client=self._off_grid_client(drop_m1_bucket=True))
        with self.assertLogs(longbridge.log, "WARNING"):
            rows = p.minute_history("hk06127", "m30", "2026-10-05 09:30", "2026-10-05 16:00", now=self.LATER)
        slots = [r.slot_end[11:] for r in rows]
        self.assertEqual((len(slots), "14:00" in slots, "13:30" in slots), (10, False, True))

    @staticmethod
    def _first_bucket_client(drop_opening_minute=False):
        rec = json.loads((FIX / "m30-firstbucket-1888-20260717.json").read_text())["rows"]
        by = {(period, adjust): rec[f"{period}-{adjust}"] for period in ("m30", "m1", "day") for adjust in ("raw", "qfq")}
        if drop_opening_minute:                  # 2026-05-19 的形状：同源 m1 也缺 09:30 那一分钟
            for adjust in ("raw", "qfq"):
                by[("m1", adjust)] = [r for r in by[("m1", adjust)] if r["timestamp"][11:16] != "09:30"]
        return FakeClient(by_date=by, live=rec["m30-raw"])

    def test_missing_opening_bucket_filled_from_m1(self):
        # 1888.HK 2026-07-17 实测：m30 缺 09:30 起点那根（10:00 槽），所在月份转 known_gap；同源 m1 有完整的
        # 09:30–09:59，按桶聚合补上。m1 不含开市竞价量，补出的槽量偏少（同日 700.HK 对照差 433,402 股）
        client = self._first_bucket_client()
        p = longbridge.LongbridgeProvider(client=client)
        with self.assertLogs(longbridge.log, "WARNING"):
            rows = p.minute_history("hk01888", "m30", "2026-07-17 09:30", "2026-07-17 16:00", now=self.LATER)
        got = {r.slot_end[11:]: (r.open, r.high, r.low, r.close, r.volume) for r in rows}
        self.assertEqual(list(got), ["10:00", "10:30", "11:00", "11:30", "12:00", "13:30",
                                     "14:00", "14:30", "15:00", "15:30", "16:00"])
        self.assertEqual(got["10:00"], (47.0, 47.88, 45.52, 46.90, 29186935))     # m1 09:30–09:59 聚合
        self.assertEqual(got["10:30"], (46.90, 48.32, 46.76, 46.94, 11950525))    # 已有的 m30 保留原值
        self.assertEqual([c[2:] for c in client.calls if c[2] == "m1"], [("m1", "raw", "2026-07-17", "2026-07-17")])
        qfq = [b["slot_end"][11:] for b in p.qfq_series("hk01888", "m30", "2026-07-17 09:30", "2026-07-17 16:00")]
        self.assertEqual(qfq, list(got))         # 前复权缓存发布前要求 raw 的槽它都有

    def test_missing_bucket_without_opening_minute_in_m1_stays_missing(self):
        # 2026-05-19 的形状：m1 也缺桶起点那一分钟，聚合出的是残缺桶（量与最低价对不上日线），不补
        p = longbridge.LongbridgeProvider(client=self._first_bucket_client(drop_opening_minute=True))
        rows = p.minute_history("hk01888", "m30", "2026-07-17 09:30", "2026-07-17 16:00", now=self.LATER)
        self.assertEqual([r.slot_end[11:] for r in rows][:2], ["10:30", "11:00"])

    def test_live_m30_does_not_fetch_m1_for_missing_bucket(self):
        # 盘中增量每轮都取：不为缺桶多发 m1 请求，缺的槽交给定稿与续传（minute_history）
        client = self._first_bucket_client()
        p = longbridge.LongbridgeProvider(client=client)
        rows = p.minute_live("hk01888", "m30", now=datetime(2026, 7, 17, 16, 30))
        self.assertEqual((len(rows), [c for c in client.calls if c[2] == "m1"]), (10, []))

    def test_clean_m30_fetch_requests_no_day_bars(self):
        _, c = self.m30()
        self.assertEqual({call[2] for call in c.calls}, {"m30"})

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

    def test_missing_credentials_is_non_self_healing(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            for name in longbridge._CRED_NAMES:
                os.environ.pop(name, None)
            with self.assertRaises(raw.ProviderAuthError) as ctx:
                longbridge.SdkClient()._context()
        self.assertEqual(ctx.exception.vendor_code, "未设置")

    def test_vendor_401_codes_are_non_self_healing_and_echoed(self):
        rec = json.loads((ERR / "longbridge-401004-token.json").read_text())

        class OpenApiException(Exception):        # 与 SDK 异常同形：code、message、trace_id 属性
            def __init__(self):
                super().__init__(f"OpenApiException: (code={rec['code']}) {rec['message']}")
                self.code, self.message, self.trace_id = rec["code"], rec["message"], rec["trace_id"]

        class Rejected:
            def history_by_date(self, *args):
                raise OpenApiException()

        with self.assertRaises(raw.ProviderAuthError) as ctx:
            longbridge.LongbridgeProvider(client=Rejected()).day_history("hk00700", "2026-05-14", "2026-05-15")
        self.assertEqual((ctx.exception.vendor_code, ctx.exception.vendor_message), ("401004", "token invalid"))

    def test_other_vendor_codes_stay_ordinary(self):
        class Limited:
            def history_by_date(self, *args):
                exc = RuntimeError("OpenApiException: (code=301600) the interval must be less than one month")
                exc.code, exc.message = 301600, "the interval must be less than one month"
                raise exc

        with self.assertRaises(raw.ProviderError) as ctx:
            longbridge.LongbridgeProvider(client=Limited()).day_history("hk00700", "2026-05-14", "2026-05-15")
        self.assertNotIsInstance(ctx.exception, raw.ProviderAuthError)


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
