"""冷备 raw provider：BaoStock 个股日线与日历、Yahoo 港股日线与 m30（spec §5.2、§8；计划 A 决定 11）。

录制 fixture 驱动，不联网、不导入 baostock / curl_cffi。
"""
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
import types
import unittest
from datetime import datetime
from pathlib import Path

from chanapp.engine.kline.providers import baostock_raw, raw, yahoo_raw

FIX = Path(__file__).resolve().parent / "fixtures" / "kline_raw"


def _load(*parts):
    return json.loads(FIX.joinpath(*parts).read_text())


class FakeBaostock:
    """模拟 BaoStock 查询：按函数名分派，返回 (fields, rows)。"""

    def __init__(self, day=None, dates=None):
        self.day, self.dates, self.calls = day, dates, []

    def __call__(self, name, **params):
        self.calls.append((name, params))
        rec = self.day if name == "query_history_k_data_plus" else self.dates
        return rec["fields"], rec["rows"]


class BaostockRawTests(unittest.TestCase):
    def test_unadjusted_request_and_row_mapping(self):
        q = FakeBaostock(day=_load("baostock", "day-600036-202409.json"))
        rows = baostock_raw.BaostockRawProvider(query=q).day_history("sh600036", "2024-09-01", "2024-09-30")
        name, params = q.calls[0]
        self.assertEqual(name, "query_history_k_data_plus")
        self.assertEqual((params["code"], params["adjustflag"], params["frequency"]), ("sh.600036", "3", "d"))
        self.assertEqual(params["fields"], "date,open,high,low,close,preclose,volume,amount,tradestatus")
        first = rows[0]
        self.assertEqual((first.trade_date, first.open, first.pc, first.volume),
                         ("2024-09-02", 32.0, 32.15, 55802065.0))
        self.assertEqual({(r.volume_unit, r.currency, r.provenance, r.sf) for r in rows},
                         {("share", "CNY", "final", 0)})

    def test_suspended_rows_get_sf_1_and_empty_strings_become_none(self):
        q = FakeBaostock(day=_load("baostock", "day-688981-suspended.json"))
        rows = baostock_raw.BaostockRawProvider(query=q).day_history("sh688981", "2025-08-27", "2025-09-05")
        by_date = {r.trade_date: r for r in rows}
        self.assertEqual(by_date["2025-08-29"].sf, 0)
        suspended = by_date["2025-09-01"]
        self.assertEqual((suspended.sf, suspended.volume, suspended.amount, suspended.pc), (1, None, None, 114.76))

    def test_rows_outside_requested_range_raise(self):
        q = FakeBaostock(day=_load("baostock", "day-600036-202409.json"))
        with self.assertRaises(raw.ProviderRangeError):
            baostock_raw.BaostockRawProvider(query=q).day_history("sh600036", "2024-09-10", "2024-09-20")

    def test_index_is_not_in_baostock_scope(self):
        q = FakeBaostock(day=_load("baostock", "day-600036-202409.json"))
        with self.assertRaises(raw.ProviderUnsupported):
            baostock_raw.BaostockRawProvider(query=q).day_history("sh000001", "2024-09-01", "2024-09-30")
        self.assertEqual(q.calls, [])

    def test_calendar_from_trade_dates(self):
        q = FakeBaostock(dates=_load("baostock", "tradedates-2024.json"))
        rows = baostock_raw.BaostockRawProvider(query=q).calendar(2024)
        self.assertEqual(q.calls[0], ("query_trade_dates", {"start_date": "2024-01-01", "end_date": "2024-12-31"}))
        by_date = {r.date: r for r in rows}
        self.assertEqual(len(rows), 366)
        self.assertTrue(by_date["2024-01-02"].is_open)
        self.assertFalse(by_date["2024-10-01"].is_open)
        self.assertEqual(by_date["2024-01-02"].market, "CN")

    def test_query_failure_is_provider_error(self):
        def broken(name, **params):
            raise RuntimeError("baostock 查询失败 10002007 网络接收错误")
        with self.assertRaises(raw.ProviderError):
            baostock_raw.BaostockRawProvider(query=broken).day_history("sh600036", "2024-09-01", "2024-09-30")

    def test_connection_class_failures_are_connection_errors(self):
        # 冷备连接/超时类失败不得消耗缺口重试次数：与主源一样抛 ProviderConnectionError
        for exc in (TimeoutError("watchdog"), ConnectionResetError("reset"),
                    RuntimeError("baostock query_history_k_data_plus 失败: 10002007 网络接收错误")):
            def broken(name, _exc=exc, **params):
                raise _exc
            with self.subTest(exc=exc), self.assertRaises(raw.ProviderConnectionError):
                baostock_raw.BaostockRawProvider(query=broken).day_history("sh600036", "2024-09-01", "2024-09-30")

    def test_non_connection_failures_stay_plain_provider_errors(self):
        def broken(name, **params):
            raise RuntimeError("baostock query_history_k_data_plus 失败: 10004011 参数错误")
        with self.assertRaises(raw.ProviderError) as cm:
            baostock_raw.BaostockRawProvider(query=broken).day_history("sh600036", "2024-09-01", "2024-09-30")
        self.assertNotIsInstance(cm.exception, raw.ProviderConnectionError)


class _FakeResultSet:
    def __init__(self, rows, error_code="0", error_msg=""):
        self.fields = ["calendar_date", "is_trading_day"]
        self.error_code, self.error_msg, self._rows = error_code, error_msg, list(rows)

    def next(self):
        return bool(self._rows)

    def get_row_data(self):
        return self._rows.pop(0)


class _FakeBaostockLib:
    """注入 sys.modules 的假 baostock 库：query_trade_dates 按 plan 逐次决定行为。

    plan 取值：ok 正常返回；hang 阻塞到测试释放；sock 阻塞在真实 socket recv 上（与库的 send_msg 同形）；
    neterr 返回网络类错误码。logout_blocks=True 时 logout 在死连接上收包（阻塞到释放）。
    """

    def __init__(self, plan, logout_blocks=False):
        self.plan, self.logout_blocks = list(plan), logout_blocks
        self.logins = self.logouts = self.queries = 0
        self.release = threading.Event()
        self.sockets, self.workers_done = [], []
        self.context = types.ModuleType("baostock.common.context")
        self.common = types.ModuleType("baostock.common")
        self.common.context = self.context
        self.module = types.ModuleType("baostock")
        self.module.common = self.common
        self.module.login, self.module.logout = self.login, self.logout
        self.module.query_trade_dates = self.query_trade_dates

    def install(self, test):
        saved = {k: sys.modules.get(k) for k in ("baostock", "baostock.common", "baostock.common.context")}
        sys.modules.update({"baostock": self.module, "baostock.common": self.common,
                            "baostock.common.context": self.context})

        def restore():
            self.release.set()
            for s in self.sockets:          # shutdown 才能唤醒别的线程里阻塞的 recv
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        test.addCleanup(restore)
        return self

    def login(self):
        self.logins += 1
        a, b = socket.socketpair()
        self.sockets += [a, b]
        self.context.default_socket = a
        return types.SimpleNamespace(error_code="0", error_msg="")

    def logout(self):
        self.logouts += 1
        if self.logout_blocks:
            self.release.wait()
        return types.SimpleNamespace(error_code="0", error_msg="")

    def query_trade_dates(self, **params):
        self.queries += 1
        mode = self.plan.pop(0)
        if mode == "hang":
            self.release.wait()
            return _FakeResultSet([])
        if mode == "sock":
            done = threading.Event()
            self.workers_done.append(done)
            try:
                sock = self.context.default_socket
                while True:                 # 库的 send_msg：recv 无超时，读到结尾标记才退出
                    if sock.recv(8192).endswith(b"<![CDATA[]]>\n"):
                        break
            finally:
                done.set()
        if mode == "neterr":
            return _FakeResultSet([], error_code="10002007", error_msg="网络接收错误。")
        return _FakeResultSet([["2024-01-02", "1"]])


def _bounded(fn, limit=3.0):
    """在线程里调用 fn，最多等 limit 秒：返回 ("ok", 值) / ("exc", 异常) / ("hung", None)。"""
    box = {}

    def run():
        try:
            box["ok"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["exc"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(limit)
    if t.is_alive():
        return "hung", None
    return ("ok", box["ok"]) if "ok" in box else ("exc", box["exc"])


def _query():
    return baostock_raw.default_query("query_trade_dates", start_date="2024-01-01", end_date="2024-12-31")


class BaostockWatchdogTests(unittest.TestCase):
    """看门狗行为（计划 A 结束审查 P2-14）：一次挂起不得让进程内的冷备永久不可用。"""
    TIMEOUT = 0.3

    def setUp(self):
        saved = {k: getattr(baostock_raw, k) for k in ("QUERY_TIMEOUT", "_logged_in")}
        baostock_raw.QUERY_TIMEOUT = self.TIMEOUT
        baostock_raw._logged_in = False
        stuck = getattr(baostock_raw, "_stuck", None)
        if stuck is not None:
            stuck.clear()

        def restore():
            for k, v in saved.items():
                setattr(baostock_raw, k, v)
            if stuck is not None:
                stuck.clear()
        self.addCleanup(restore)

    def test_healthy_query_after_one_hang_succeeds(self):
        lib = _FakeBaostockLib(["hang", "ok"]).install(self)
        state, exc = _bounded(_query)
        self.assertEqual(state, "exc")
        self.assertIsInstance(exc, TimeoutError)
        state, value = _bounded(_query)
        self.assertEqual(state, "ok", f"挂起一次后健康查询应成功，实际 {state} {value!r}")
        self.assertEqual(value, (["calendar_date", "is_trading_day"], [["2024-01-02", "1"]]))

    def test_recovery_does_not_block_on_dead_connection(self):
        # 恢复不能在死连接上登出：logout 会在同一个全局 socket 上收包而阻塞，并占住串行锁
        lib = _FakeBaostockLib(["hang", "ok"], logout_blocks=True).install(self)
        state, _ = _bounded(_query)
        self.assertEqual(state, "exc", "超时后恢复过程挂住了调用方")
        state, value = _bounded(_query)
        self.assertEqual(state, "ok", f"恢复后查询应成功，实际 {state} {value!r}")

    def test_repeated_hangs_cap_stuck_workers_then_recover(self):
        cap = getattr(baostock_raw, "MAX_STUCK_WORKERS", 2)
        lib = _FakeBaostockLib(["hang"] * cap + ["ok"]).install(self)
        for _ in range(cap):
            self.assertEqual(_bounded(_query)[0], "exc")
        t0 = time.monotonic()
        state, exc = _bounded(lambda: baostock_raw.BaostockRawProvider().calendar(2024))
        elapsed = time.monotonic() - t0
        self.assertEqual(state, "exc")
        self.assertIsInstance(exc, raw.ProviderConnectionError)
        self.assertLess(elapsed, self.TIMEOUT / 2, "卡死线程达上限后应快速失败，不再新建线程等超时")
        self.assertEqual(lib.queries, cap)
        lib.release.set()                               # 卡住的查询返回 → 线程退出 → 恢复
        deadline = time.monotonic() + 2
        while any(t.is_alive() for t in getattr(baostock_raw, "_stuck", [])) and time.monotonic() < deadline:
            time.sleep(0.01)
        state, value = _bounded(_query)
        self.assertEqual(state, "ok", f"卡死线程退出后应恢复，实际 {state} {value!r}")

    def test_worker_blocked_on_socket_recv_exits_after_discard(self):
        lib = _FakeBaostockLib(["sock"]).install(self)
        state, exc = _bounded(_query)
        self.assertEqual(state, "exc")
        self.assertIsInstance(exc, TimeoutError)
        self.assertTrue(lib.workers_done[0].wait(1.0), "丢弃会话后阻塞在 recv 上的工作线程应在 1 秒内退出")

    def test_non_timeout_failure_discards_session(self):
        lib = _FakeBaostockLib(["neterr", "ok"]).install(self)
        state, exc = _bounded(_query)
        self.assertEqual(state, "exc")
        self.assertIn("10002007", str(exc))
        state, _ = _bounded(_query)
        self.assertEqual(state, "ok")
        self.assertEqual(lib.logins, 2, "失败后的下一次查询应重新登录")

    def test_process_exits_with_a_stuck_query(self):
        # 卡死的查询线程不得拖住解释器退出（systemd 停服务会一直等到 SIGKILL）
        child = textwrap.dedent("""
            import sys, threading, types
            never = threading.Event()
            ctx = types.ModuleType("baostock.common.context")
            common = types.ModuleType("baostock.common"); common.context = ctx
            bs = types.ModuleType("baostock"); bs.common = common
            bs.login = lambda: types.SimpleNamespace(error_code="0", error_msg="")
            bs.logout = lambda: types.SimpleNamespace(error_code="0", error_msg="")
            bs.query_trade_dates = lambda **p: never.wait()
            sys.modules.update({"baostock": bs, "baostock.common": common, "baostock.common.context": ctx})
            from chanapp.engine.kline.providers import baostock_raw
            baostock_raw.QUERY_TIMEOUT = 0.2
            try:
                baostock_raw.default_query("query_trade_dates", start_date="2024-01-01", end_date="2024-12-31")
            except Exception as e:
                print("returned", type(e).__name__)
        """)
        root = Path(__file__).resolve().parents[2]
        env = {**os.environ, "PYTHONPATH": str(root)}
        try:
            out = subprocess.run([sys.executable, "-c", child], cwd=root, env=env,
                                 capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("有卡死查询时进程 10 秒内未退出")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("returned TimeoutError", out.stdout)


class FakeYahoo:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def __call__(self, sym, params):
        self.calls.append((sym, params))
        return self.payload


class YahooRawTests(unittest.TestCase):
    NOW = datetime(2026, 9, 28, 10, 0)

    def test_symbol_mapping_is_four_digit(self):
        self.assertEqual(yahoo_raw._yahoo_sym("hk00700"), "0700.HK")
        self.assertEqual(yahoo_raw._yahoo_sym("hk06166"), "6166.HK")
        self.assertEqual(yahoo_raw._yahoo_sym("hk00005"), "0005.HK")   # 5 位会被当成别的交易所的基金
        self.assertEqual(yahoo_raw._yahoo_sym("hk09988"), "9988.HK")

    def test_request_has_no_adjust_or_event_params(self):
        fetch = FakeYahoo(_load("yahoo", "chart-06166-30m.json"))
        yahoo_raw.YahooRawProvider(fetch=fetch).minute_history(
            "hk06166", "m30", "2026-09-21 09:30", "2026-09-22 16:00", now=self.NOW)
        sym, params = fetch.calls[0]
        self.assertEqual((sym, params["interval"]), ("6166.HK", "30m"))
        self.assertNotIn("events", params)
        self.assertNotIn("includeAdjustedClose", params)

    def test_start_labels_shift_by_30_minutes(self):
        fetch = FakeYahoo(_load("yahoo", "chart-06166-30m.json"))
        rows = yahoo_raw.YahooRawProvider(fetch=fetch).minute_history(
            "hk06166", "m30", "2026-09-21 09:30", "2026-09-22 16:00", now=self.NOW)
        day = [r for r in rows if r.trade_date == "2026-09-22"]
        self.assertEqual([r.slot_end[11:] for r in day],
                         ["10:00", "10:30", "11:00", "11:30", "12:00", "13:30",
                          "14:00", "14:30", "15:00", "15:30", "16:00"])
        self.assertEqual({(r.state, r.volume_unit, r.trade_state) for r in rows}, {("closed", "share", "traded")})

    def test_off_grid_rows_dropped(self):
        fetch = FakeYahoo(_load("yahoo", "chart-00700-30m-handwritten.json"))
        rows = yahoo_raw.YahooRawProvider(fetch=fetch).minute_history(
            "hk00700", "m30", "2026-09-22 09:30", "2026-09-22 16:00", now=self.NOW)
        self.assertEqual([r.slot_end for r in rows], ["2026-09-22 13:30", "2026-09-22 14:00"])

    def test_closing_auction_merges_into_16_00_slot(self):
        # 与长桥同一规则（spec §5.2）：丢弃竞价会让 m30 极值与量和日线对不上
        fetch = FakeYahoo(_load("yahoo", "chart-06166-30m.json"))
        rows = yahoo_raw.YahooRawProvider(fetch=fetch).minute_history(
            "hk06166", "m30", "2026-09-21 09:30", "2026-09-22 16:00", now=self.NOW)
        last = [r for r in rows if r.slot_end == "2026-09-21 16:00"]
        self.assertEqual(len(last), 1)
        self.assertAlmostEqual(last[0].open, 126.5)
        self.assertAlmostEqual(last[0].close, 126.3, places=4)
        self.assertEqual(last[0].volume, 438610 + 48800)

    def test_none_price_rows_dropped(self):
        payload = _load("yahoo", "chart-06166-30m.json")
        res = payload["chart"]["result"][0]
        quote = res["indicators"]["quote"][0]
        # 让一根网格内的 bar（2026-09-22 10:00 起点 → 10:30 槽）缺收盘价
        i = next(i for i, t in enumerate(res["timestamp"])
                 if yahoo_raw._hkt(t).strftime("%Y-%m-%d %H:%M") == "2026-09-22 10:00")
        quote["close"][i] = None
        rows = yahoo_raw.YahooRawProvider(fetch=FakeYahoo(payload)).minute_history(
            "hk06166", "m30", "2026-09-21 09:30", "2026-09-22 16:00", now=self.NOW)
        slots = {r.slot_end for r in rows}
        self.assertNotIn("2026-09-22 10:30", slots)
        self.assertIn("2026-09-22 11:00", slots)
        self.assertTrue(all(None not in (r.open, r.high, r.low, r.close) for r in rows))

    def test_rows_after_now_are_not_closed_history(self):
        fetch = FakeYahoo(_load("yahoo", "chart-06166-30m.json"))
        rows = yahoo_raw.YahooRawProvider(fetch=fetch).minute_history(
            "hk06166", "m30", "2026-09-21 09:30", "2026-09-22 16:00", now=datetime(2026, 9, 22, 11, 5))
        self.assertEqual(max(r.slot_end for r in rows), "2026-09-22 11:00")

    def test_day_rows_raw_with_previous_close(self):
        fetch = FakeYahoo(_load("yahoo", "chart-06166-1d.json"))
        rows = yahoo_raw.YahooRawProvider(fetch=fetch).day_history("hk06166", "2026-09-10", "2026-09-22")
        self.assertEqual(fetch.calls[0][1]["interval"], "1d")
        self.assertEqual(rows[0].trade_date, "2026-09-10")
        self.assertIsNotNone(rows[0].pc)            # 回看窗口提供首行前收
        for prev, cur in zip(rows, rows[1:]):
            self.assertEqual(cur.pc, prev.close)
        self.assertEqual({(r.currency, r.volume_unit, r.sf, r.provenance) for r in rows},
                         {("HKD", "share", 0, "final")})

    def test_granularity_mismatch_is_provider_error(self):
        payload = _load("yahoo", "chart-06166-30m.json")
        payload["chart"]["result"][0]["meta"]["dataGranularity"] = "1d"
        with self.assertRaises(raw.ProviderError):
            yahoo_raw.YahooRawProvider(fetch=FakeYahoo(payload)).minute_history(
                "hk06166", "m30", "2026-09-21 09:30", "2026-09-22 16:00", now=self.NOW)

    def test_injected_fetch_connection_failure_is_connection_error(self):
        def down(sym, params):
            raise TimeoutError("read timeout")
        with self.assertRaises(raw.ProviderConnectionError):
            yahoo_raw.YahooRawProvider(fetch=down).day_history("hk06166", "2026-09-10", "2026-09-22")

    def default_fetch_with(self, responses):
        """default_fetch 走假 session：responses 逐次返回（异常或 (status, json)），不导入 curl_cffi。"""
        from unittest import mock

        class Resp:
            def __init__(self, status):
                self.status_code = status

            def raise_for_status(self):
                if self.status_code >= 400:
                    err = RuntimeError(f"HTTP {self.status_code}")
                    err.response = self
                    raise err

            def json(self):
                return {}

        seq = iter(responses)

        class Session:
            def get(self, url, params=None, timeout=None):
                item = next(seq)
                if isinstance(item, BaseException):
                    raise item
                return Resp(item)

        with mock.patch.object(yahoo_raw, "_get_session", lambda refresh=False: Session()), \
                mock.patch.object(yahoo_raw.time, "sleep", lambda s: None):
            return yahoo_raw.default_fetch("0700.HK", {"interval": "1d"})

    def test_default_fetch_classifies_final_failure(self):
        n = yahoo_raw.FETCH_ATTEMPTS
        with self.assertRaises(raw.ProviderConnectionError):
            self.default_fetch_with([TimeoutError("t")] * n)
        with self.assertRaises(raw.ProviderConnectionError):
            self.default_fetch_with([429] * n)                  # 限流：源级问题，不消耗缺口次数
        with self.assertRaises(raw.ProviderServerError):
            self.default_fetch_with([502] * n)
        with self.assertRaises(raw.ProviderError) as cm:
            self.default_fetch_with([404] * n)
        self.assertNotIsInstance(cm.exception, (raw.ProviderConnectionError, raw.ProviderServerError))

    def test_only_m30_minutes(self):
        fetch = FakeYahoo(_load("yahoo", "chart-06166-30m.json"))
        with self.assertRaises(raw.ProviderUnsupported):
            yahoo_raw.YahooRawProvider(fetch=fetch).minute_history(
                "hk06166", "m5", "2026-09-21 09:30", "2026-09-22 16:00", now=self.NOW)


if __name__ == "__main__":
    unittest.main()
