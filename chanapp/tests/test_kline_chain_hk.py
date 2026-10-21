"""港股完整链路（计划 A 的 A4 退出证据）：长桥录制 fixture → 采集器 → 准入 → 事实库 → 视图。

不联网、不读凭据、不导入 SDK：注入读录制文件的 client，并让任何 socket 连接直接失败。
样本：700.HK 在 2026-05-15 除净日前后（raw 与供应商前复权成对录制）。录制的 m30 只有 05-14、05-15 两天：
其余交易日槽位不全，分钟缺口保持打开；05-22 没有分钟，定稿不算成功（spec §5.3 完整性、计划 A 的 A3 定稿规则）。
"""
import json
import os
import socket
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from chanapp.engine.kline import collector, facts, views
from chanapp.engine.kline.providers import longbridge

FIX = Path(__file__).resolve().parent / "fixtures" / "kline_raw" / "longbridge"
CODE = "hk00700"
NOW = datetime(2026, 5, 22, 17, 0)          # 周五收盘后、定稿时点


def _rows(name):
    return json.loads((FIX / name).read_text())["rows"]


class RecordedClient:
    def __init__(self):
        self.by = {("day", "raw"): _rows("day-raw-700.json"), ("day", "qfq"): _rows("day-qfq-700.json"),
                   ("m30", "raw"): _rows("m30-raw-700.json"), ("m30", "qfq"): _rows("m30-qfq-700.json")}

    def history_by_date(self, symbol, period, adjust, start, end):
        assert symbol == "700.HK"
        return [r for r in self.by.get((period, adjust), []) if start <= r["timestamp"][:10] <= end]

    def candlesticks(self, symbol, period, count, adjust):
        return []


def _no_network(*args, **kwargs):
    raise AssertionError("链路测试不得联网")


def _run_chain(cls, client):
    """回填日线与分钟 → 定稿 → 供应商前复权缓存；结果与事实库连接挂到测试类上。"""
    cls.tmp = tempfile.TemporaryDirectory()
    with mock.patch.dict(os.environ, {}, clear=False), \
            mock.patch.object(socket.socket, "connect", _no_network):
        for name in ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN"):
            os.environ.pop(name, None)
        c = collector.Collector(cls.tmp.name,
                                providers={"longbridge": longbridge.LongbridgeProvider(client=client)},
                                clock=lambda: NOW.timestamp(), watchlist_fn=lambda: [CODE])
        c.backfill_day(CODE)
        c.plan_minute_backfill(CODE)
        cls.drain = c.drain_gaps(max_requests=100)
        cls.finalize = c.finalize_due([CODE], "2026-05-22", NOW, calendar_known=True)
        cls.vendor = c.refresh_vendor_qfq(CODE, NOW, closed_through="2026-05-22")
    cls.conn = facts.open_facts(Path(cls.tmp.name) / facts.DB_NAME)


class HKChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _run_chain(cls, RecordedClient())

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def view(self, freq, adjust):
        return views.read_view(self.conn, CODE, freq, adjust=adjust, limit=5000, now=NOW)

    def test_sdk_never_imported(self):
        self.assertNotIn("longbridge.openapi", sys.modules)

    def test_raw_day_is_unadjusted_with_previous_close(self):
        view = self.view("day", "raw")
        closes = {b["dt"]: b["close"] for b in view.bars}
        self.assertEqual((len(closes), closes["2026-05-14"], closes["2026-05-15"]), (11, 460.2, 456.4))
        row = next(r for r in facts.read_day_rows(self.conn, CODE) if r["trade_date"] == "2026-05-15")
        self.assertEqual((row["pc"], row["volume_unit"]), (460.2, "share"))
        self.assertEqual(view.source, "longbridge")
        self.assertFalse(view.degraded)

    def test_raw_m30_has_eleven_bars_and_matches_day_volume(self):
        view = self.view("m30", "raw")
        for day in ("2026-05-14", "2026-05-15"):
            bars = [b for b in view.bars if b["dt"][:10] == day]
            self.assertEqual(len(bars), 11)
            self.assertEqual(bars[-1]["dt"], f"{day} 16:00")
            day_row = next(r for r in facts.read_day_rows(self.conn, CODE) if r["trade_date"] == day)
            self.assertEqual(sum(b["volume"] for b in bars), day_row["volume"])
            self.assertEqual(max(b["high"] for b in bars), day_row["high"])
            self.assertEqual(min(b["low"] for b in bars), day_row["low"])

    def test_raw_m60_aggregates_m30_on_hk_clock(self):
        m30 = [b for b in self.view("m30", "raw").bars if b["dt"][:10] == "2026-05-14"]
        m60 = [b for b in self.view("m60", "raw").bars if b["dt"][:10] == "2026-05-14"]
        self.assertEqual(sum(b["volume"] for b in m60), sum(b["volume"] for b in m30))
        self.assertEqual((m60[-1]["dt"], m60[-1]["close"]), (m30[-1]["dt"], m30[-1]["close"]))

    def test_finalize_without_recorded_minutes_is_not_done(self):
        self.assertEqual((self.finalize["done"], self.finalize["failed"]), ([], [CODE]))
        gaps = {(g["dataset"], g["reason"]) for g in facts.open_gaps(self.conn, CODE)
                if g["start"].startswith("2026-05-22")}
        self.assertEqual(gaps, {("day", "finalize"), ("m30", "finalize")})

    def test_vendor_qfq_cache_published(self):
        self.assertEqual((self.vendor["day"], self.vendor["m30"]), (1, 1))
        view = self.view("day", "qfq")
        closes = {b["dt"]: b["close"] for b in view.bars}
        self.assertEqual(view.adjust_label, "前复权（供应商口径）")
        self.assertAlmostEqual(closes["2026-05-14"], 454.908)      # 除净日前一日按供应商口径调整
        self.assertAlmostEqual(closes["2026-05-15"], 456.4)        # 除净日及之后与 raw 相同
        m60 = self.view("m60", "qfq")
        self.assertTrue(m60.bars)
        self.assertNotEqual(m60.token, self.view("m60", "raw").token)

    def test_hk_calendar_derived_from_day_backfill(self):
        from chanapp.engine.kline import calendar
        days = calendar.trading_days(self.conn, "HK", "2026-05-08", "2026-05-22")
        self.assertEqual(len(days), 11)
        self.assertEqual(self.view("week", "raw").incomplete_days, [])

    def test_minute_gaps_drained_without_failures(self):
        self.assertEqual(self.drain["failed"], 0)
        self.assertFalse(self.drain["budget_exhausted"])
        # 录制只覆盖 2 个交易日：取数成功但槽位不全，缺口保持打开并计一次尝试，不当作已补齐
        self.assertEqual(self.drain["incomplete"], 1)
        backfill = [g for g in facts.open_gaps(self.conn, CODE, "m30") if g["reason"] == "backfill"]
        self.assertEqual([g["attempts"] for g in backfill], [1])


class OpeningAuctionClient(RecordedClient):
    """05-14 09:30 起的 m30 改成生产实测的形状（6127.HK 2024-12-10、2025-03-06）：开盘价 = 日线开盘价（竞价成交），
    持续交易的低高不含它且越出超过竞价补齐容差。"""
    SHAPE = {"raw": {"low": "479.000", "close": "479.200"},      # 开 474.200 低于低 1.01%
             "qfq": {"low": "473.500", "close": "473.700"}}      # 开 468.747 低于低 1.00%+

    def __init__(self):
        super().__init__()
        for adjust, fields in self.SHAPE.items():
            self.by[("m30", adjust)] = [{**r, **fields} if r["timestamp"] == "2026-05-14T09:30:00" else r
                                        for r in self.by[("m30", adjust)]]


class HKChainOpeningAuctionTests(unittest.TestCase):
    """回归（2026-10-12 起 hk06127 前复权停在 10-09）：一根越界超出容差的开盘竞价 bar 让 raw 拒收该槽，
    并让供应商前复权 day+m30 整组不发布。日线佐证后该槽入库、缓存照常发布。"""

    @classmethod
    def setUpClass(cls):
        _run_chain(cls, OpeningAuctionClient())

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def bar(self, adjust):
        view = views.read_view(self.conn, CODE, "m30", adjust=adjust, limit=5000, now=NOW)
        return next(b for b in view.bars if b["dt"] == "2026-05-14 10:00"), view

    def test_raw_opening_slot_admitted_with_low_widened_to_auction_open(self):
        bar, view = self.bar("raw")
        self.assertEqual((bar["open"], bar["high"], bar["low"], bar["close"]), (474.2, 479.6, 474.2, 479.2))
        self.assertEqual(len([b for b in view.bars if b["dt"][:10] == "2026-05-14"]), 11)

    def test_vendor_qfq_published_with_opening_slot(self):
        self.assertEqual((self.vendor["day"], self.vendor["m30"]), (1, 1))
        bar, view = self.bar("qfq")
        self.assertEqual((bar["open"], bar["low"]), (468.747, 468.747))
        self.assertFalse(view.stale)


class OffGridClient(RecordedClient):
    """05-14 13:30 起的 m30 起始标签改成 13:29（生产实测形状：6127.HK 2026-10-05），m1 给出该桶（一根等于原 bar）。"""

    def __init__(self):
        super().__init__()
        for adjust in ("raw", "qfq"):
            rows = self.by[("m30", adjust)]
            bar = next(r for r in rows if r["timestamp"] == "2026-05-14T13:30:00")
            self.by[("m30", adjust)] = [{**r, "timestamp": "2026-05-14T13:29:00"} if r is bar else r for r in rows]
            self.by[("m1", adjust)] = [bar]


class HKChainOffGridLabelTests(unittest.TestCase):
    """回归（hk06127 2024-05-29、2024-11-06、2026-10-05 缺槽，所在月份转 known_gap）：不在半点上的起始标签被当作
    会话外丢弃，该槽永远缺失。改由当日 m1 聚合补槽后，raw 与供应商前复权都有完整 11 根。"""

    @classmethod
    def setUpClass(cls):
        _run_chain(cls, OffGridClient())

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def day_bars(self, adjust):
        view = views.read_view(self.conn, CODE, "m30", adjust=adjust, limit=5000, now=NOW)
        return {b["dt"][11:]: b for b in view.bars if b["dt"][:10] == "2026-05-14"}

    def test_raw_slot_filled(self):
        src = next(r for r in _rows("m30-raw-700.json") if r["timestamp"] == "2026-05-14T13:30:00")
        bars = self.day_bars("raw")
        self.assertEqual(len(bars), 11)
        self.assertEqual((bars["14:00"]["open"], bars["14:00"]["close"], bars["14:00"]["volume"]),
                         (float(src["open"]), float(src["close"]), float(src["volume"])))

    def test_vendor_qfq_published_with_full_day(self):
        self.assertEqual((self.vendor["day"], self.vendor["m30"]), (1, 1))
        self.assertEqual(len(self.day_bars("qfq")), 11)


if __name__ == "__main__":
    unittest.main()
