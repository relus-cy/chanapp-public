"""门面贯通（计划 B 单元 I）：engine.data 与 API 在真实事实库上的契约——字段、首取失败、令牌与 409、
bundle、计算审计、交易日历钩子。事实库放临时目录，采集器关闭（不访问上游）。"""
import functools
import json
import unittest
from datetime import date, datetime, time, timedelta
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.engine import compute_cache, data as engine_data, data_identity, session
from chanapp.engine.kline import calendar, collector, config, facts, hk_vendor_qfq, sessions, views
from chanapp.engine.kline.rows import CalendarRow, RawDayRow, RawMinuteRow, new_batch_id
from chanapp.tests import cache_support
from chanapp.tests.test_kline_collector import FACT

CODE = "sh600926"
KW = dict(market="CN", kind="stock", source="mairui", binding_gen=1)
CONTRACT_KEYS = {"code", "freq", "adjust", "bars", "token", "source", "degraded", "degraded_note", "fqf",
                 "fetch_time", "from_cache", "stale", "stale_age_s", "data_version", "incomplete_days",
                 "volume_unit", "coverage", "notices", "has_more", "oldest_dt"}


def weekdays(n, end):
    out, day = [], end
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day -= timedelta(days=1)
    return sorted(out)


class FacadeBase(unittest.TestCase):
    def setUp(self):
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        tmp = cache_support.temp_dir(self)
        cache_support.isolate_cache_dir(self, tmp)
        cache_support.set_env(self, "ANALYSIS_CACHE_DIR", tmp)
        compute_cache.clear()
        self.addCleanup(compute_cache.clear)
        self.today = date.today().isoformat()
        self.days = weekdays(60, date.today() - timedelta(days=7))
        with self.writer() as conn:
            calendar.store_rows(conn, [CalendarRow("CN", d, True) for d in self.days], source="t")

    def writer(self):
        return engine_data._collector().writer()

    def commit_days(self, days, close=10.0, prov="final"):
        with self.writer() as conn:
            rows = [RawDayRow(CODE, d, close, close * 1.1, close * 0.98, close + (i % 5) * 0.1, 1000.0, "lot",
                              1.0, "CNY", close, 0, prov, new_batch_id()) for i, d in enumerate(days)]
            return facts.commit_day_rows(conn, rows, item="day_history", today=self.today, **KW)

    def quarantine_days(self, days):
        with self.writer() as conn, facts.write_txn(conn):
            for d in days:
                facts.quarantine(conn, CODE, "day", d, "review_conflict")

    def commit_minutes(self, day, state="closed"):
        slots = sessions.slots("CN", FACT)
        with self.writer() as conn:
            rows = [RawMinuteRow(CODE, day, f"{day} {t}", 10.0, 10.0 + (i % 7) * 0.05, 9.9, 10.0 + (i % 5) * 0.03,
                                 10.0, "lot", 1.0, state, "traded", new_batch_id()) for i, t in enumerate(slots)]
            return facts.commit_minute_rows(conn, rows, item="minute_history", fact_freq=FACT,
                                            today=self.today, **KW)

    def pin_view_now(self, hhmm):
        """把视图时钟钉在今天的 hhmm（本地 naive，与 datetime.now() 同形），今天显式记为开市日（不靠工作日回落）。
        门面的 stale/fetch_time 数据集随会话阶段切换（盘中日线取分钟事实），不钉住则结果随跑测试的时刻变。"""
        with self.writer() as conn:
            calendar.store_rows(conn, [CalendarRow("CN", self.today, True)], source="t")
        now = datetime.combine(date.fromisoformat(self.today), time.fromisoformat(hhmm))
        patch = mock.patch.object(views, "read_view", functools.partial(views.read_view, now=now))
        patch.start()
        self.addCleanup(patch.stop)

    def last_commit_at(self, dataset):
        return facts.last_commit_at(engine_data._reader(), CODE, dataset)


class FacadeContractTests(FacadeBase):
    def test_get_bars_returns_contract_fields_in_shares(self):
        self.pin_view_now("08:00")                                    # 开盘前：日线的新鲜度取日线数据集
        self.commit_days(self.days)
        out = engine_data.get_bars(CODE, "day", adjust="raw")
        self.assertEqual(set(out), CONTRACT_KEYS)
        self.assertEqual((out["adjust"], out["fqf"], out["volume_unit"], out["from_cache"]),
                         ("raw", "不复权", "share", True))
        self.assertEqual(out["bars"][-1]["volume"], 100000)          # 1000 手 → 股
        self.assertEqual(set(out["bars"][-1]), {"dt", "open", "high", "low", "close", "volume"})
        self.assertIsNotNone(out["fetch_time"])
        self.assertEqual(out["fetch_time"], self.last_commit_at("day"))
        self.assertEqual((out["stale"], out["stale_age_s"]), (False, None))   # 上一交易日已定稿

    def test_in_session_day_freshness_follows_minute_facts(self):
        # 盘中日线的当日 bar 来自分钟，stale 与 fetch_time 都取分钟事实数据集（契约「新鲜度」）；
        # 只有日线提交、没有任何分钟提交时 fetch_time 为 null 且 stale，不回落到日线的提交时刻
        self.pin_view_now("10:30")
        self.commit_days(self.days)
        out = engine_data.get_bars(CODE, "day", adjust="raw")
        self.assertEqual(set(out), CONTRACT_KEYS)
        self.assertEqual(out["bars"][-1]["dt"], self.days[-1])
        self.assertEqual((out["fetch_time"], out["stale"], out["stale_age_s"]), (None, True, None))
        with self.writer() as conn:                  # 提交时刻精度为秒：把日线提交挪到上一交易日，两个数据集才可区分
            conn.execute("UPDATE series_state SET last_commit_at=? WHERE code=? AND dataset='day'",
                         (f"{self.days[-1]}T15:30:00+08:00", CODE))
        self.commit_minutes(self.today, state="forming")
        out = engine_data.get_bars(CODE, "day", adjust="raw")
        self.assertEqual(out["fetch_time"], self.last_commit_at(FACT))
        self.assertNotEqual(out["fetch_time"], self.last_commit_at("day"))

    def test_missing_symbol_with_collector_off_is_data_unavailable(self):
        with self.assertRaises(engine_data.DataUnavailable):
            engine_data.get_bars("sz000001", "day")

    def test_history_token_follows_closed_facts_not_forming_updates(self):
        self.commit_days(self.days[:-1])
        token = engine_data.get_bars(CODE, "day", adjust="raw")["token"]
        page = engine_data.get_bars_history(CODE, "day", self.days[-3], 10, adjust="raw", token=token)
        self.assertEqual(page["bars"][-1]["dt"], self.days[-4])
        self.commit_minutes(self.today, state="forming")          # 盘中 forming 不改令牌
        self.assertEqual(engine_data.get_bars(CODE, "day", adjust="raw")["token"], token)
        self.commit_days(self.days[-1:])                          # 新的已收盘事实：旧令牌分页 409
        with self.assertRaises(engine_data.TokenMismatch) as raised:
            engine_data.get_bars_history(CODE, "day", self.days[-3], 10, adjust="raw", token=token)
        self.assertNotEqual(raised.exception.token, token)
        with self.assertRaises(engine_data.TokenMismatch):
            engine_data.get_bars_history(CODE, "day", self.days[-3], 10, adjust="raw", token=None)

    def test_bundle_serves_three_periods_with_distinct_tokens(self):
        self.commit_days(self.days)
        for d in self.days[-5:]:
            self.commit_minutes(d)
        bundle = engine_data.get_bars_bundle(CODE, ("day", "m60", "m30"), adjust="raw")
        self.assertEqual(set(bundle), {"day", "m60", "m30"})
        self.assertTrue(all(bundle[f]["bars"] for f in bundle))
        self.assertEqual(len({bundle[f]["token"] for f in bundle}), 3)

    def test_record_calc_run_writes_facts_store(self):
        result = engine_data.record_calc_run(CODE, "day", input_start=self.days[0], input_end=self.days[-1],
                                             input_data_version="v", calculation_id="c", signals=[])
        self.assertTrue(result["recorded"])
        n = engine_data._reader().execute("SELECT COUNT(*) FROM calc_runs WHERE code=?", (CODE,)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_calendar_hook_closes_session_on_known_holiday(self):
        holiday = date(2026, 10, 1)
        with self.writer() as conn:
            calendar.store_rows(conn, [CalendarRow("CN", holiday.isoformat(), False)], source="t")
        from datetime import datetime
        self.assertFalse(session.is_session_open(datetime(2026, 10, 1, 10, 0), "cn"))
        self.assertTrue(session.is_session_open(datetime(2026, 10, 12, 10, 0), "cn"))   # 未知日回落工作日

    def test_served_facts_never_trigger_first_fetch(self):
        # 迁自旧门面「缓存命中不抓取」：事实库已有可服务数据时不走同步首取
        self.commit_days(self.days)
        with mock.patch.object(engine_data._collector(), "ensure_window",
                               side_effect=AssertionError("已有事实不应首取")):
            self.assertTrue(engine_data.get_bars(CODE, "day", adjust="raw")["from_cache"])

    def test_unknown_freq_or_adjust_rejected(self):
        for freq, adjust in (("m1", "qfq"), ("day", "hfq")):
            with self.subTest(freq=freq, adjust=adjust), self.assertRaises(ValueError):
                engine_data.get_bars(CODE, freq, adjust=adjust)

    def test_data_version_stable_for_same_view_and_split_by_adjust(self):
        self.commit_days(self.days)
        first = engine_data.get_bars(CODE, "day", adjust="raw")
        again = engine_data.get_bars(CODE, "day", adjust="raw")
        qfq = engine_data.get_bars(CODE, "day", adjust="qfq")
        self.assertEqual(first["data_version"], again["data_version"])
        self.assertNotEqual(first["data_version"], qfq["data_version"])


    def test_history_old_token_after_full_quarantine_is_mismatch_not_empty(self):
        # 只有日线的标的被整段隔离：旧令牌分页仍按令牌不符处理（带当前令牌），不当成「无数据」
        self.commit_days(self.days)
        token = engine_data.get_bars(CODE, "day", adjust="raw")["token"]
        self.quarantine_days(self.days)
        with self.assertRaises(engine_data.TokenMismatch) as raised:
            engine_data.get_bars_history(CODE, "day", self.days[-3], 10, adjust="raw", token=token)
        self.assertIsInstance(raised.exception.token, str)
        self.assertNotEqual(raised.exception.token, token)
        from chanapp.api.main import app
        response = TestClient(app).get(f"/api/chart?code={CODE}&freq=day&adjust=raw"
                                       f"&before={self.days[-3]}&token={token}")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["token"], raised.exception.token)


class HkFirstOpenTests(FacadeBase):
    """港股默认前复权读供应商缓存：缓存未建是「数据暂缺」，走同步首取，不是「不支持」。"""
    HK = "hk00700"

    def setUp(self):
        super().setUp()
        self.hk_days = self.days[-5:]
        with self.writer() as conn:
            calendar.store_rows(conn, [CalendarRow("HK", d, True) for d in self.hk_days], source="t")
            facts.commit_day_rows(conn, [RawDayRow(self.HK, d, 100.0, 101.0, 99.0, 100.0, 1000, "share", None,
                                                   "HKD", 100.0, 0, "final", new_batch_id())
                                         for d in self.hk_days],
                                  market="HK", kind="stock", item="day_history", source="longbridge",
                                  binding_gen=1, today=self.today)

    def publish_cache(self, *_args, **_kw):
        with self.writer() as conn:
            hk_vendor_qfq.publish(conn, self.HK, "day", [
                {"trade_date": d, "open": 50.0, "high": 51.0, "low": 49.0, "close": 50.0, "volume": 1000.0,
                 "amount": None, "volume_unit": "share"} for d in self.hk_days], closed_through=self.hk_days[-1])
        return True

    def test_missing_vendor_cache_triggers_first_fetch_then_serves_qfq(self):
        with mock.patch.object(engine_data._collector(), "ensure_window",
                               side_effect=self.publish_cache) as ensure:
            out = engine_data.get_bars(self.HK, "day")
        ensure.assert_called_once_with(self.HK, "day", wait=config.REQUEST_LOCK_WAIT_S)
        self.assertEqual([b["close"] for b in out["bars"]], [50.0] * 5)
        self.assertEqual((out["fqf"], out["from_cache"]), ("前复权（供应商口径）", False))
        self.assertNotIn("unsupported", [n["code"] for n in out["notices"]])

    def test_vendor_cache_still_missing_after_first_fetch_is_data_unavailable(self):
        with mock.patch.object(engine_data._collector(), "ensure_window", return_value=False) as ensure, \
             self.assertRaises(engine_data.DataUnavailable):
            engine_data.get_bars(self.HK, "day")
        ensure.assert_called_once_with(self.HK, "day", wait=config.REQUEST_LOCK_WAIT_S)

    def test_finer_than_fact_period_stays_unsupported_without_fetch(self):
        with mock.patch.object(engine_data._collector(), "ensure_window",
                               side_effect=AssertionError("不支持的周期不应首取")):
            out = engine_data.get_bars(self.HK, "m5")
        self.assertEqual((out["bars"], [n["code"] for n in out["notices"]]), ([], ["unsupported"]))

    def test_hk_m5_m15_unsupported_in_both_adjust_modes(self):
        # 港股分钟事实为 m30：前复权（供应商缓存）与不复权（raw 事实聚合）两条路径都不从 m30 伪造细周期
        with mock.patch.object(engine_data._collector(), "ensure_window", return_value=True) as ensure:
            for freq in ("m5", "m15"):
                for adjust in ("qfq", "raw"):
                    with self.subTest(freq=freq, adjust=adjust):
                        out = engine_data.get_bars(self.HK, freq, adjust=adjust)
                        self.assertEqual((out["bars"], [n["code"] for n in out["notices"]]),
                                         ([], ["unsupported"]))
                        bundle = engine_data.get_bars_bundle(self.HK, (freq,), adjust=adjust, primary=freq)
                        self.assertEqual([n["code"] for n in bundle[freq]["notices"]], ["unsupported"])
        ensure.assert_not_called()                  # 不支持的周期不首取


def short_view(freq, n, *, has_more=False, code=CODE):
    bars = [{"dt": f"2026-01-01 {i:05d}", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1,
             "forming": False} for i in range(n)]
    return views.View(code=code, freq=freq, adjust="qfq", bars=bars, token=f"t-{freq}-{n}", adjust_label="前复权",
                      source="s", coverage={}, has_more=has_more)


class ShortWindowTests(unittest.TestCase):
    """视图短于默认窗口且没有更早数据：门面对该周期补一次窗口；同一 (code, freq) 10 分钟内至多一次。"""

    def setUp(self):
        # 只测补窗口：采集器关闭，请求路径追赶（D4）不运行（替身采集器的返回值不代表追赶结果）
        cache_support.set_env(self, "COLLECTOR_ENABLED", "0")
        self.clock = [1000.0]
        for patch in (mock.patch.object(engine_data, "_clock", lambda: self.clock[0], create=True),
                      mock.patch.object(engine_data, "_reader", return_value=None),
                      mock.patch.object(engine_data, "_window_attempts", {}, create=True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.collector = mock.Mock()
        patch = mock.patch.object(engine_data, "_collector", return_value=self.collector)
        patch.start()
        self.addCleanup(patch.stop)

    def read_view(self, n, **kw):
        return mock.patch.object(engine_data.views, "read_view", return_value=short_view("m60", n, **kw))

    def test_short_view_triggers_one_ensure_then_throttles_for_ten_minutes(self):
        with self.read_view(260):
            engine_data.get_bars(CODE, "m60")
            self.clock[0] += 300
            engine_data.get_bars(CODE, "m60")
            self.assertEqual(self.collector.ensure_window.call_args_list, [mock.call(CODE, "m60", wait=config.REQUEST_LOCK_WAIT_S)])
            self.clock[0] += 301
            engine_data.get_bars(CODE, "m60")
        self.assertEqual(self.collector.ensure_window.call_count, 2)

    def test_full_window_or_more_history_does_not_trigger(self):
        with self.read_view(engine_data.N_BARS):
            engine_data.get_bars(CODE, "m60")
        with self.read_view(260, has_more=True):
            engine_data.get_bars(CODE, "m60")
        self.collector.ensure_window.assert_not_called()

    def test_short_view_rereads_after_ensure(self):
        reads = [short_view("m60", 260), short_view("m60", engine_data.N_BARS)]
        with mock.patch.object(engine_data.views, "read_view", side_effect=reads):
            out = engine_data.get_bars(CODE, "m60")
        self.assertEqual(len(out["bars"]), engine_data.N_BARS)
        self.assertTrue(out["from_cache"])

    def test_bundle_ensures_short_periods_once_and_rereads(self):
        def bundle(_conn, code, freqs, *, adjust):
            return {f: short_view(f, 260 if f == "m60" else engine_data.N_BARS) for f in freqs}
        with mock.patch.object(engine_data.views, "read_bundle", side_effect=bundle) as read:
            engine_data.get_bars_bundle(CODE, ("day", "m60", "m30"))
            self.assertEqual(read.call_count, 2)
            engine_data.get_bars_bundle(CODE, ("day", "m60", "m30"))
        self.assertEqual(self.collector.ensure_window.call_args_list, [mock.call(CODE, "m60", wait=config.REQUEST_LOCK_WAIT_S)])


    def test_busy_code_serves_the_short_snapshot_and_reports_busy_only_without_one(self):
        # 第四阶段复核必须修：首开等同一代码的锁超过预算（FlightBusy）时不再取数——短视图照常返回现有快照，
        # 没有可服务快照才抛 RefetchBusy（API 回 503，页面受控重试）
        self.collector.ensure_window.side_effect = collector.FlightBusy(CODE)
        with self.read_view(260):
            self.assertEqual(len(engine_data.get_bars(CODE, "m60")["bars"]), 260)
        with mock.patch.object(engine_data.views, "read_view", return_value=None), \
                self.assertRaises(engine_data.RefetchBusy):
            engine_data.get_bars(CODE, "m60")


class AuxiliaryFetchFailureTests(FacadeBase):
    """主图可读、辅助周期首取抛异常：主图照常 200，共振缺该周期；主周期首取抛异常仍 502。"""

    def setUp(self):
        super().setUp()
        self.commit_days(self.days)                               # 只有日线：m60/m30 视图为空，走首取
        from chanapp.api.main import app
        self.client = TestClient(app)

    def chart(self, freq, failing):
        def ensure(code, f, *_a, **_kw):
            if f == failing:
                raise RuntimeError("upstream down")
            return False

        with mock.patch.object(engine_data._collector(), "ensure_window", side_effect=ensure) as spy:
            return self.client.get(f"/api/chart?code={CODE}&freq={freq}"), spy

    def test_auxiliary_first_fetch_failure_keeps_main_chart(self):
        with self.assertLogs("chanapp.engine.data", level="WARNING"):   # 辅助周期失败只记日志
            response, spy = self.chart("day", "m60")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body["kline"]), len(self.days))
        freqs = [r["freq"] for r in body["resonance"]]
        self.assertIn("day", freqs)
        self.assertNotIn("m60", freqs)
        self.assertLessEqual({"m60", "m30"}, {c.args[1] for c in spy.call_args_list})   # m30 照常补取

    def test_primary_first_fetch_failure_is_502(self):
        response, _ = self.chart("m60", "m60")
        self.assertEqual(response.status_code, 502)

    def test_bundle_without_primary_keeps_raising(self):
        # AI 走 read_bundle（不给 primary）：任一周期首取失败照旧上抛，由 read_bundle 转成全 None
        with mock.patch.object(engine_data._collector(), "ensure_window", side_effect=RuntimeError("down")), \
             self.assertRaises(RuntimeError):
            engine_data.get_bars_bundle(CODE, ("day", "m60", "m30"))


class DataVersionTests(unittest.TestCase):
    """data_version 的内容身份（迁自旧供数测试 DataIdentityTests，去掉供数身份）。"""

    def test_canonical_keys_complete_content_and_identity(self):
        bars = [{"dt": "a", "close": 10}, {"dt": "b", "close": 20}]
        original = data_identity.version(bars, {"token": "t", "adjust": "qfq"})
        self.assertRegex(original, r"^[0-9a-f]{64}$")
        self.assertEqual(original, data_identity.version([{"close": 10, "dt": "a"}, {"close": 20, "dt": "b"}],
                                                         {"adjust": "qfq", "token": "t"}))
        self.assertNotEqual(original, data_identity.version(bars, {"token": "t", "adjust": "raw"}))
        self.assertNotEqual(data_identity.version(bars), data_identity.version(list(reversed(bars))))
        self.assertNotEqual(data_identity.version([]), data_identity.version(bars))
        bars[0]["close"] = 11
        self.assertNotEqual(original, data_identity.version(bars, {"token": "t", "adjust": "qfq"}))


class FacadeApiTests(FacadeBase):
    def setUp(self):
        super().setUp()
        cache_support.set_env(self, "LLM_API_KEY", "k")
        self.commit_days(self.days)
        for d in self.days[-5:]:
            self.commit_minutes(d)
        from chanapp.api.main import app
        self.client = TestClient(app)

    def analyze(self, tokens):
        params = {"code": CODE, "freq": "day"}
        if tokens is not None:
            params["tokens"] = json.dumps(tokens)
        return self.client.get("/api/analysis", params=params)

    def test_chart_then_analysis_then_closed_fact_invalidates(self):
        chart = self.client.get(f"/api/chart?code={CODE}&freq=day")
        self.assertEqual(chart.status_code, 200, chart.text)
        meta = chart.json()["meta"]
        self.assertFalse({"scheme", "generation", "epoch", "cache_ttl", "history_cursor", "basis"} & set(meta))
        tokens = meta["analysis_tokens"]
        self.assertTrue(all(tokens[f] for f in ("day", "m60", "m30")))
        with mock.patch("chanapp.api.analysis.engine_llm.analyze",
                        return_value='{"current_state": "s", "scenarios": []}') as llm:
            self.assertEqual(self.analyze(tokens).status_code, 200)
            self.commit_minutes(self.days[-6])                    # 同一标的新增已收盘事实
            stale = self.analyze(tokens)
            missing = self.analyze(None)
        self.assertEqual(stale.status_code, 409)
        self.assertNotEqual(stale.json()["tokens"], tokens)
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(llm.call_count, 1)

    def test_analysis_old_tokens_after_quarantine_is_409_with_current_tokens(self):
        # 分析周期被整段隔离：旧令牌先按不符 409（带当前视图令牌），令牌相符而数据为空才 502
        tokens = self.client.get(f"/api/chart?code={CODE}&freq=day").json()["meta"]["analysis_tokens"]
        self.quarantine_days(self.days)
        with mock.patch("chanapp.api.analysis.engine_llm.analyze",
                        return_value='{"current_state": "s", "scenarios": []}') as llm:
            stale = self.analyze(tokens)
            self.assertEqual(stale.status_code, 409, stale.text)
            current = stale.json()["tokens"]
            self.assertIsInstance(current["day"], str)
            self.assertNotEqual(current["day"], tokens["day"])
            self.assertEqual(self.analyze(current).status_code, 502)
        llm.assert_not_called()

    def test_chart_main_view_and_analysis_tokens_come_from_one_read(self):
        # 第一次读取之后立即提交新的已收盘事实：主图与 analysis_tokens 不得分属两个版本
        state = {"reads": 0}

        def then_commit(fn):
            def wrapped(*args, **kwargs):
                out = fn(*args, **kwargs)
                state["reads"] += 1
                if state["reads"] == 1:
                    self.commit_minutes(self.days[-6])
                return out
            return wrapped

        with mock.patch.object(engine_data.views, "read_view", then_commit(views.read_view)), \
             mock.patch.object(engine_data.views, "read_bundle", then_commit(views.read_bundle)):
            chart = self.client.get(f"/api/chart?code={CODE}&freq=day")
        self.assertEqual(chart.status_code, 200, chart.text)
        meta = chart.json()["meta"]
        self.assertEqual(meta["token"], meta["analysis_tokens"]["day"])

    def test_history_page_409_after_closed_fact(self):
        chart = self.client.get(f"/api/chart?code={CODE}&freq=day&adjust=raw").json()
        url = f"/api/chart?code={CODE}&freq=day&adjust=raw&before={self.days[-3]}&token={chart['meta']['token']}"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.commit_days([self.days[-1]], close=11.0)             # final 冲突进待核验，令牌不变
        self.assertEqual(self.client.get(url).status_code, 200)
        with self.writer() as conn:
            review = conn.execute("SELECT review_id FROM pending_review").fetchone()["review_id"]
            with facts.write_txn(conn):
                facts.resolve_review(conn, review, "accept")      # 接受修订：已收盘代次前进
        conflict = self.client.get(url)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(set(conflict.json()), {"detail", "token"})

    def test_chart_publication_records_calc_run(self):
        self.assertEqual(self.client.get(f"/api/chart?code={CODE}&freq=day").status_code, 200)
        freqs = {r[0] for r in engine_data._reader().execute(
            "SELECT freq FROM calc_runs WHERE code=?", (CODE,))}
        self.assertIn("day", freqs)

    def test_calc_run_failure_does_not_block_chart(self):
        with mock.patch.object(engine_data, "record_calc_run", side_effect=RuntimeError("db down")), \
             self.assertLogs("chanapp.engine.chart_payload", level="WARNING"):
            response = self.client.get(f"/api/chart?code={CODE}&freq=day")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("signals", response.json())

    def test_status_endpoint_uses_real_collector_view(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()),
                         {"checked_at", "mode", "enabled", "datasets", "probes", "budget", "calendar_export"})
        self.assertEqual(response.json()["mode"], "unconfigured")  # 直接库级测试没有进入应用 lifespan
        self.assertFalse(response.json()["enabled"])


if __name__ == "__main__":
    unittest.main()
