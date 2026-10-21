"""提醒线读取：水平线标为提醒线后，GET /api/alert-lines 给出换算到当前原始价口径的线价与同一次读取的最新价
（真实事实库与画线库，采集器关闭，视图时钟钉在 2026-09-28 10:00 盘中）。

实现前列出的失败方式：
- 前复权下画的线在新除权后仍按旧口径给出（读取方拿原始价比旧前复权线价，误报或漏报）；
- 港股按供应商前复权口径给出，没有按参照日换算；
- 指数按画线时记的复权模式换算（指数恒为原始价，不该乘任何比例）；
- 参照日已不在前复权覆盖区时编造一个价位，而不是给空；
- 已删除的线、没有 alert 的线出现在结果里；非水平线或方向非法的 alert 被存下；
- 最新价与 /api/quotes 不是同一口径，stale 被吞掉或改写；按 code 过滤时混入其他标的。
"""
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.engine import data as engine_data
from chanapp.engine.kline import calendar, facts, hk_vendor_qfq, views
from chanapp.engine.kline.rows import CalendarRow, RawDayRow, RawMinuteRow, new_batch_id
from chanapp.tests import cache_support

DAYS = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28"]
TODAY = "2026-09-28"
NOW = datetime(2026, 9, 28, 10, 0)


class PinnedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


def day_row(code, d, close, pc, prov="final", currency="CNY"):
    if prov == "preopen":
        return RawDayRow(code, d, None, None, None, None, None, "lot", None, currency, pc, 0, prov, new_batch_id())
    return RawDayRow(code, d, close, close, close, close, 1000.0, "lot", 1.0, currency, pc, 0, prov, new_batch_id())


def hline(price, *, adjust="qfq", ref=None, alert="below", kind="horizontal-line"):
    body = {"v": 1, "kind": kind, "adjust": adjust, "anchors": [{"freq": "day", "dt": "2026-09-24", "price": price}],
            "style": {}, "ref": ref}
    if alert is not None:
        body["alert"] = {"direction": alert}
    return body


class AlertLinesApiTests(unittest.TestCase):
    def setUp(self):
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        tmp = Path(cache_support.temp_dir(self))
        cache_support.set_env(self, "WATCHLIST_PATH", str(tmp / "w.json"))
        cache_support.set_env(self, "DRAWINGS_PATH", "")
        cache_support.isolate_cache_dir(self, tmp)
        patch = mock.patch.object(views, "datetime", PinnedDatetime)
        patch.start()
        self.addCleanup(patch.stop)
        with engine_data._collector().writer() as conn:
            calendar.store_rows(conn, [CalendarRow(m, d, True) for m in ("CN", "HK") for d in DAYS], source="t")
            # A 股个股：09-25 收盘 20.0，今天盘前参考价 16.0（新除权，之前各日前复权乘 0.8）；盘中 09:45 一根 16.2
            cn = [day_row("sh600926", d, c, pc) for d, c, pc in
                  (("2026-09-21", 19.8, 19.7), ("2026-09-22", 19.9, 19.8), ("2026-09-23", 20.0, 19.9),
                   ("2026-09-24", 20.0, 20.0), ("2026-09-25", 20.0, 20.0))]
            facts.commit_day_rows(conn, cn, market="CN", kind="stock", item="day_history", source="mairui",
                                  binding_gen=1, today=TODAY)
            facts.commit_day_rows(conn, [day_row("sh600926", TODAY, None, 16.0, prov="preopen")], market="CN",
                                  kind="stock", item="preopen_ref", source="mairui", binding_gen=1, today=TODAY)
            facts.commit_minute_rows(conn, [RawMinuteRow("sh600926", TODAY, f"{TODAY} 09:45", 16.1, 16.3, 16.0, 16.2,
                                                         100.0, "lot", 1.0, "closed", "traded", new_batch_id())],
                                     market="CN", kind="stock", item="minute_live", fact_freq="m15", source="mairui",
                                     binding_gen=1, today=TODAY)
            fresh = (NOW - timedelta(seconds=60)).astimezone().isoformat(timespec="seconds")
            conn.execute("UPDATE series_state SET last_commit_at=? WHERE code=? AND dataset='m15'",
                         (fresh, "sh600926"))
            conn.commit()
            # 指数：只有日线，盘中没有分钟（最新价为昨收，stale）
            idx = [day_row("sh000001", d, c, None) for d, c in
                   (("2026-09-23", 3000.0), ("2026-09-24", 3010.0), ("2026-09-25", 3020.5))]
            facts.commit_day_rows(conn, idx, market="CN", kind="index", item="day_history", source="mairui",
                                  binding_gen=1, today=TODAY)
            # 港股：原始收盘恒 400；09-25 除净，供应商前复权 09-24 及以前为 396（等比 0.99）；盘中没有分钟（stale）
            hk = [day_row("hk00700", d, 400.0, 400.0, currency="HKD") for d in DAYS[:5]]
            facts.commit_day_rows(conn, hk, market="HK", kind="stock", item="day_history", source="longbridge",
                                  binding_gen=1, today=TODAY)
            vendor = [{"trade_date": d, "open": c, "high": c, "low": c, "close": c, "volume": 100.0}
                      for d, c in zip(DAYS[:5], (396.0, 396.0, 396.0, 396.0, 400.0))]
            hk_vendor_qfq.publish(conn, "hk00700", "day", vendor, closed_through="2026-09-25")
        from chanapp.api.main import app
        self.c = TestClient(app)

    def put(self, code, id_, body, status=200):
        r = self.c.put(f"/api/drawings/{code}/{id_}", json={"body": body, "base_revision": None})
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def alert_lines(self, query=""):
        r = self.c.get(f"/api/alert-lines{query}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        return r.json()["alert_lines"]

    def test_lines_are_converted_to_todays_raw_basis_with_the_same_read_as_quotes(self):
        self.put("sh600926", "raw-line-01", hline(17.123456, adjust="raw", alert="above"))
        self.put("sh600926", "qfq-line-01", hline(19.5, ref={"day": "2026-09-24", "close": 20.0}))
        self.put("sh600926", "old-ref-001", hline(18.0, ref={"day": "2026-08-03", "close": 21.0}))
        self.put("sh000001", "index-line1", hline(3100.0, ref={"day": "2026-09-24", "close": 3050.0}, alert="above"))
        self.put("hk00700", "hk-line-001", hline(380.0, ref={"day": "2026-09-23", "close": 400.0}))
        cn_latest = {"price": 16.2, "price_time": "2026-09-28 09:45", "stale": False}
        self.assertEqual(self.alert_lines(), [
            {"code": "hk00700", "drawing_id": "hk-line-001", "direction": "below", "price": 376.2,
             "drawn_adjust": "qfq", "drawn_price": 380.0,
             "latest": {"price": 400.0, "price_time": None, "stale": True}},
            {"code": "sh000001", "drawing_id": "index-line1", "direction": "above", "price": 3100.0,
             "drawn_adjust": "qfq", "drawn_price": 3100.0,
             "latest": {"price": 3020.5, "price_time": None, "stale": True}},
            {"code": "sh600926", "drawing_id": "raw-line-01", "direction": "above", "price": 17.1235,
             "drawn_adjust": "raw", "drawn_price": 17.1235, "latest": cn_latest},
            {"code": "sh600926", "drawing_id": "qfq-line-01", "direction": "below", "price": 15.6,
             "drawn_adjust": "qfq", "drawn_price": 19.5, "latest": cn_latest},
            # 参照日已不在前复权覆盖区：无法换算，给空，不编造
            {"code": "sh600926", "drawing_id": "old-ref-001", "direction": "below", "price": None,
             "drawn_adjust": "qfq", "drawn_price": 18.0, "latest": cn_latest},
        ])
        quotes = self.c.get("/api/quotes?code=hk00700").json()["quotes"]
        self.assertEqual({k: quotes["hk00700"][k] for k in ("price", "price_time", "stale")},
                         {"price": 400.0, "price_time": None, "stale": True})
        self.assertEqual([x["drawing_id"] for x in self.alert_lines("?code=hk00700")], ["hk-line-001"])

    def test_deleted_unmarked_and_invalid_lines_are_left_out(self):
        self.put("sh600926", "plain-line1", hline(19.0, adjust="raw", alert=None))
        gone = self.put("sh600926", "gone-line01", hline(19.1, adjust="raw"))
        self.assertEqual(self.c.delete(f"/api/drawings/sh600926/gone-line01?base_revision={gone['revision']}")
                         .status_code, 200)
        self.put("sh600926", "trend-line1", hline(19.2, adjust="raw", kind="trend-line"), status=422)
        self.put("sh600926", "cross-line1", hline(19.3, adjust="raw", alert="cross"), status=422)
        self.put("sh600926", "kept-line01", hline(19.4, adjust="raw"))
        self.assertEqual([x["drawing_id"] for x in self.alert_lines()], ["kept-line01"])
        self.assertEqual(self.alert_lines("?code=hk00700"), [])
        self.assertEqual(self.c.get("/api/alert-lines?code=bad").status_code, 422)


if __name__ == "__main__":
    unittest.main()
