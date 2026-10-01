"""一次性导入入口（issue #37）：CSV → 同一准入 → 采集器单写者 → /api/chart。

对照输入是麦蕊录制 fixture（上证指数与 300209 的 2024Q4 日线、600036 在 2026-09-29 的原生 m15）。
导入侧读 tests/fixtures/ingest/cn-example.csv（由同一录制逐字段转写，见 _recorded_lines）；
provider 侧把录制响应交给真实 provider，经采集器的日线回填与分钟缺口续传写入另一个库。
两边都关掉采集（COLLECTOR_ENABLED=0），只经 /api/chart 读出比较。不联网。
"""
import contextlib
import csv
import io
import json
import os
import socket
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.engine import data as engine_data
from chanapp.engine.kline import admission, calendar, collector, facts, ingest, instance
from chanapp.engine.kline.providers.mairui import MairuiProvider
from chanapp.tests import cache_support
from chanapp.tests.test_kline_collector import Clock

FIX = Path(__file__).resolve().parent / "fixtures"
EXAMPLE = FIX / "ingest" / "cn-example.csv"
RECORDED = (("sh000001", "day", "idx-day-000001-2024q4.json"),
            ("sz300209", "day", "day-300209-2024q4.json"),
            ("sh600036", "m15", "m15-600036-20260929.json"))
ROUTES = {"/hsindex/history/000001.SH/d/{L}": "idx-day-000001-2024q4.json",
          "/hsstock/history/300209.SZ/d/n/{L}": "day-300209-2024q4.json",
          "/hsstock/history/600036.SH/15/n/{L}": "m15-600036-20260929.json"}
NOW = datetime(2026, 10, 1, 10, 0)


def _recording(name):
    return json.loads((FIX / "kline_raw" / "mairui" / name).read_text())


def _recorded_lines():
    """录制响应逐字段转写成导入行（独立于产品代码的转写，作为「同一输入」的依据）。"""
    for code, freq, name in RECORDED:
        for r in _recording(name)["body"]:
            yield {"code": code, "freq": freq, "dt": r["t"][:10] if freq == "day" else r["t"][:16],
                   "open": r["o"], "high": r["h"], "low": r["l"], "close": r["c"], "volume": r["v"],
                   "volume_unit": "lot", "amount": r.get("a"),
                   "pc": r.get("pc") if freq == "day" else None, "suspended": int(r.get("sf", 0))}


def _recorded_transport(path, params, timeout):
    if path not in ROUTES:
        return 404, {}
    rec = _recording(ROUTES[path])
    return rec.get("status", 200), rec["body"]


class _IngestCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict("os.environ", {"COLLECTOR_ENABLED": "0"}))
        self.enterContext(mock.patch.object(socket.socket, "connect",
                                            side_effect=AssertionError("导入测试不得联网")))
        self.addCleanup(collector._shared.clear)
        self.addCleanup(engine_data._window_attempts.clear)
        self.clock = Clock(NOW.timestamp())

    def fresh_dir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def worker(self, root, providers=None):
        worker = collector.Collector(root, providers=providers or {}, clock=self.clock)
        self.addCleanup(lambda: worker.conn().close())
        return worker

    def chart(self, root, code, freq, adjust):
        cache_support.isolate_cache_dir(self, root)
        engine_data._window_attempts.clear()
        from chanapp.api.main import app
        r = TestClient(app).get(f"/api/chart?code={code}&freq={freq}&adjust={adjust}")
        self.assertEqual(r.status_code, 200, r.text[:300])
        return r.json()


class ImportMatchesProviderPath(_IngestCase):
    def provider_path(self):
        root = self.fresh_dir()
        provider = MairuiProvider(transport=_recorded_transport, licence="")
        worker = self.worker(root, {"mairui": provider})
        for code in ("sh000001", "sz300209"):
            self.assertIn("inserted", worker.backfill_day(code))
        with worker.writer() as conn, facts.write_txn(conn):
            facts.record_gap(conn, "sh600036", "m15", "2026-09-29 09:30", "2026-09-29 15:00", "backfill")
        self.assertEqual(worker.drain_gaps(1, code="sh600036")["done"], 1)
        return root

    def test_example_file_is_the_recorded_input(self):
        with EXAMPLE.open(newline="") as f:
            rows = list(csv.DictReader(f))
        expected = list(_recorded_lines())
        self.assertEqual(len(rows), len(expected))
        for got, want in zip(rows, expected):
            for key, value in want.items():
                text = got[key]
                if value is None:
                    self.assertEqual(text, "", (key, got))
                elif isinstance(value, str):
                    self.assertEqual(text, value, (key, got))
                else:
                    self.assertEqual(float(text), value, (key, got))

    def test_imported_bars_match_provider_path_bar_by_bar(self):
        provider_root = self.provider_path()
        import_root = self.fresh_dir()
        report = ingest.import_file(EXAMPLE, self.worker(import_root))
        self.assertEqual(report["rows"], 193)
        for code, freq, adjust in (("sh000001", "day", "raw"), ("sh000001", "week", "raw"),
                                   ("sz300209", "day", "qfq"), ("sz300209", "day", "raw"),
                                   ("sz300209", "week", "qfq"), ("sh600036", "m30", "raw"),
                                   ("sh600036", "m60", "raw")):
            with self.subTest(code=code, freq=freq, adjust=adjust):
                want = self.chart(provider_root, code, freq, adjust)
                got = self.chart(import_root, code, freq, adjust)
                self.assertTrue(want["kline"])
                self.assertEqual(got["kline"], want["kline"])
                for key in ("adjust", "fqf", "incomplete_days", "has_more", "oldest_dt"):
                    self.assertEqual(got["meta"][key], want["meta"][key], key)
                for key in ("qfq_from", "qfq_through", "stop_reason"):
                    self.assertEqual(got["meta"]["coverage"][key], want["meta"]["coverage"][key], key)
                self.assertEqual([n["code"] for n in got["meta"]["notices"]],
                                 [n["code"] for n in want["meta"]["notices"]])
                self.assertEqual(got["meta"]["source"], "import")


HEADER = ",".join(ingest.COLUMNS)
GOOD_DAY = "sz300209,day,2024-10-08,4.99,4.99,4.3,4.46,358454,lot,162622730,4.24,0"
GOOD_M15 = "sh600036,m15,2026-09-29 09:45,40.49,40.78,40.44,40.49,55724,lot,226014190,,0"


class _FileCase(_IngestCase):
    """写临时 CSV、断言整份拒绝且事实库不动的公共部分。"""

    def setUp(self):
        super().setUp()
        self.root = self.fresh_dir()
        self.target = self.worker(self.root)

    def write(self, *lines, header=HEADER):
        path = self.root / "input.csv"
        path.write_text("\n".join((header, *lines)) + "\n", encoding="utf-8")
        return path

    def assert_rejected(self, path, expected):
        with self.assertRaises(ingest.ImportRejected) as ctx:
            ingest.import_file(path, self.target)
        self.assertEqual([(p["line"], p["reason"]) for p in ctx.exception.problems], expected,
                         ctx.exception.problems)
        self.assert_nothing_written()

    def assert_nothing_written(self):
        conn = self.target.conn()
        for table in ("day_bars", "minute_bars", "batches", "calendar", "pending_review"):
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)


class ImportRejectsBadInput(_FileCase):
    """格式不符与准入拒绝：整份不落库，问题带行号与原因码。"""

    def test_header_must_match_exactly(self):
        self.assert_rejected(self.write(GOOD_DAY, header=HEADER.replace(",pc", "")), [(1, ingest.BAD_HEADER)])
        self.assert_rejected(self.write(GOOD_DAY + ",x", header=HEADER + ",note"), [(1, ingest.BAD_HEADER)])

    def test_format_errors_reject_the_whole_file(self):
        path = self.write(GOOD_DAY,
                          "600036,day,2024-10-09,1,1,1,1,1,lot,,,0",                     # 缺市场前缀
                          "sz300209,d,2024-10-09,1,1,1,1,1,lot,,,0",                     # 周期
                          "sz300209,day,2024-10-09,1,1,1,abc,1,lot,,,0",                 # 非数字
                          "sz300209,day,2024-10-09,1,1,1,1,1,lot,,,yes",                 # 停牌标志
                          "sh600036,m15,2026-09-29 10:00,1,1,1,1,1,lot,,40.5,0",         # 分钟行带 pc
                          "sz300209,day,2024-10-09,1,1,1,1,1,lot,,0",                    # 少一列
                          GOOD_M15)
        self.assert_rejected(path, [(3, ingest.BAD_CODE), (4, ingest.BAD_FREQ), (5, ingest.NOT_NUMBER),
                                    (6, ingest.BAD_FLAG), (7, ingest.PC_ON_MINUTE), (8, ingest.BAD_COLUMNS)])

    def test_admission_rejections_reject_the_whole_file(self):
        path = self.write(GOOD_DAY,
                          "sz300209,day,2024-10-09,4.4,4.5,4.3,4.4,1000,hand,,4.46,0",    # 量纲不是 lot/share
                          "sz300209,day,2024-10-10,4.4,4.5,4.3,4.4,-5,lot,,4.4,0",        # 负成交量
                          "sz300209,day,2024-10-11,4.4,4.2,4.3,4.4,1000,lot,,4.4,0",      # 高低价倒挂
                          "sz300209,day,2024-10-14,,,,,1000,lot,,4.4,0",                  # 未停牌却缺价格
                          "sz300209,day,2024-10-08,4.99,4.99,4.3,4.46,358454,lot,,4.24,0",  # 同一键重复
                          "sz300209,day,2026-10-01,4.4,4.5,4.3,4.4,1000,lot,,4.4,0",      # 今天：只收到昨天
                          "sz300209,day,2024/10/15,4.4,4.5,4.3,4.4,1000,lot,,4.4,0",      # 日期格式
                          GOOD_M15,
                          "sh600036,m15,2026-09-29 09:40,40.5,40.6,40.4,40.5,100,lot,,,0",  # 不在 m15 槽位
                          "sh600036,m15,2026-09-29 10:00,40.5,40.6,40.4,40.5,100,share,,,0")
        self.assert_rejected(path, [(2, admission.DUPLICATE_KEY), (3, admission.BAD_ENUM),
                                    (4, admission.NEGATIVE_QUANTITY), (5, admission.BAD_OHLC),
                                    (6, admission.NON_FINITE), (7, admission.DUPLICATE_KEY),
                                    (8, admission.OUT_OF_RANGE), (9, admission.OUT_OF_RANGE),
                                    (11, admission.OFF_GRID)])

    def test_minute_freq_must_equal_instance_fact_freq(self):
        path = self.write(GOOD_DAY, "sh600036,m5,2024-09-27 09:35,40,40,40,40,100,lot,,,0")
        self.assert_rejected(path, [(3, ingest.FREQ_MISMATCH)])

    def test_daily_only_instance_takes_day_rows_and_refuses_minutes(self):
        config = self.root / "instance.json"
        config.write_text(json.dumps({"markets": {"CN": {"minute_fact_freq": None}}}))
        with instance.activate(instance.load_instance(config, environ={}), self.root):
            self.assert_rejected(self.write(GOOD_DAY, GOOD_M15), [(3, ingest.NO_MINUTE_FACTS)])
            report = ingest.import_file(self.write(GOOD_DAY), self.target)
        self.assertEqual(report["codes"]["sz300209"]["day"]["inserted"], 1)

    def test_rejection_leaves_existing_facts_unchanged(self):
        ingest.import_file(EXAMPLE, self.target)
        conn = self.target.conn()
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("day_bars", "minute_bars", "batches", "calendar", "pending_review")}
        before = self.chart(self.root, "sz300209", "day", "qfq")
        with self.assertRaises(ingest.ImportRejected):
            ingest.import_file(FIX / "ingest" / "cn-rejected-example.csv", self.target)
        self.assertEqual({t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counts}, counts)
        after = self.chart(self.root, "sz300209", "day", "qfq")
        self.assertEqual((after["kline"], after["meta"]["token"]), (before["kline"], before["meta"]["token"]))

    def test_m5_instance_imports_m5_and_aggregates_like_vendor_m30(self):
        # demo 初始化（#43）用 5 分样本：实例粒度为 m5 时导入 m5，读出的 30 分与同日原生 30 分录制逐根一致
        config = self.root / "instance.json"
        config.write_text(json.dumps({"markets": {"CN": {"minute_fact_freq": "m5"}}}))
        path = self.root / "m5.csv"
        lines = [HEADER] + [f"sh600036,m5,{r['t'][:16]},{r['o']},{r['h']},{r['l']},{r['c']},{r['v']},lot,{r['a']},,"
                            f"{int(r.get('sf', 0))}" for r in _recording("m5-600036-20240927.json")["body"]]
        path.write_text("\n".join(lines) + "\n")
        with instance.activate(instance.load_instance(config, environ={}), self.root):
            report = ingest.import_file(path, self.target)
            self.assertEqual(report["codes"]["sh600036"]["m5"]["inserted"], 48)
            got = self.chart(self.root, "sh600036", "m30", "raw")["kline"]
        native = _recording("m30-600036-20240927.json")["body"]
        self.assertEqual([(b["time"], b["open"], b["high"], b["low"], b["close"], b["volume"]) for b in got],
                         [(r["t"][:16], r["o"], r["h"], r["l"], r["c"], r["v"] * 100) for r in native])

    def test_held_writer_lock_writes_nothing(self):
        other = collector.Collector(self.root)
        self.addCleanup(lambda: other.conn().close())
        with other.writer():
            with self.assertRaises(collector.CollectorLocked):
                ingest.import_file(self.write(GOOD_DAY, GOOD_M15), self.target)
        self.assert_nothing_written()


class ImportBoundaries(_FileCase):
    """复审补测：标签格式、文件规模、交易所时区，以及导入的指数日线能证明什么样的休市。

    失败方式（先列后测）：
    - 同一槽位写成空格与 T 两种标签，绕过重复检查，聚合后成交量翻倍；
    - 超大文件或大量坏行在拒绝前占满内存，问题列表无界；
    - 主机时区不是 UTC+8 时，交易所已是次日，前一天的数据被当成今天整份拒绝；
    - 文件内指数日线空档过长（不可能是假期）或个股当日有行而指数没有，仍被推成休市；
    - 导入的指数片段与库里已有指数日线不相接，中间未取过的日子被推成休市（前复权跨过未知日）。
    """

    def index_row(self, day):
        return f"sh000001,day,{day},3000,3010,2990,3000,1000,lot,,3000,0"

    def test_minute_label_must_use_a_space(self):
        path = self.write(GOOD_M15, "sh600036,m15,2026-09-29T09:45,40.49,40.78,40.44,40.49,55724,lot,,,0")
        self.assert_rejected(path, [(3, admission.OUT_OF_RANGE)])

    def test_size_limits_and_problem_sample(self):
        with mock.patch.object(ingest, "MAX_ROWS", 2):
            self.assert_rejected(self.write(GOOD_DAY, GOOD_M15, GOOD_M15.replace("09:45", "10:00")),
                                 [(4, ingest.TOO_LARGE)])
        with mock.patch.object(ingest, "MAX_BYTES", 10):
            self.assert_rejected(self.write(GOOD_DAY), [(0, ingest.TOO_LARGE)])
        path = self.write(*["600036,day,2024-10-09,1,1,1,1,1,lot,,,0"] * 150)
        with self.assertRaises(ingest.ImportRejected) as ctx:
            ingest.import_file(path, self.target)
        self.assertEqual((len(ctx.exception.problems), ctx.exception.problem_count), (100, 150))
        self.assert_nothing_written()

    def test_cutoff_uses_exchange_date_not_host_timezone(self):
        previous = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()

        def restore():
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()
        self.addCleanup(restore)
        self.clock.t = datetime(2026, 9, 30, 17, 0).timestamp()      # UTC 17:00，交易所已是 10-01 01:00
        report = ingest.import_file(self.write("sz300209,day,2026-09-30,4.4,4.5,4.3,4.4,1000,lot,,4.4,0"),
                                    self.target)
        self.assertEqual(report["codes"]["sz300209"]["day"]["inserted"], 1)

    def test_index_hole_too_long_or_contradicted_is_rejected(self):
        self.assert_rejected(self.write(self.index_row("2024-10-08"), self.index_row("2024-10-24")),
                             [(3, ingest.INDEX_HOLE)])                   # 中间 11 个工作日
        self.assert_rejected(self.write(self.index_row("2024-10-08"), self.index_row("2024-10-10"),
                                        "sz300209,day,2024-10-09,4.4,4.5,4.3,4.4,1000,lot,,4.4,0"),
                             [(4, ingest.INDEX_HOLE)])                   # 个股当日有行，指数缺

    def test_index_span_proves_holidays_but_not_the_stretch_to_existing_data(self):
        ingest.import_file(EXAMPLE, self.target)
        conn = self.target.conn()
        self.assertIs(calendar.is_trading_day(conn, "CN", "2024-10-01"), False)   # 文件内连续区间里的国庆
        older = self.root / "older.csv"
        older.write_text("\n".join((HEADER, self.index_row("2024-06-03"), self.index_row("2024-06-04"))) + "\n")
        ingest.import_file(older, self.target)
        for day in ("2024-06-05", "2024-07-15", "2024-08-30"):
            self.assertIsNone(calendar.is_trading_day(conn, "CN", day), day)
        gaps = [(g["start"], g["end"], g["reason"]) for g in facts.open_gaps(conn, "sh000001", "day")]
        self.assertEqual(gaps, [("2024-06-05", "2024-08-30", "backfill")])     # 交给采集器续传
        self.assertIs(calendar.is_trading_day(conn, "CN", "2024-10-01"), False)


class ImportRevisions(_IngestCase):
    def test_reimport_is_idempotent_and_conflicts_go_to_review(self):
        root = self.fresh_dir()
        target = self.worker(root)
        ingest.import_file(EXAMPLE, target)
        before = self.chart(root, "sz300209", "day", "qfq")

        again = ingest.import_file(EXAMPLE, target)
        self.assertEqual(again["codes"]["sz300209"]["day"],
                         {"inserted": 0, "revised": 0, "skipped": 78 + 1, "pending_review": 0, "rejected": 0})
        self.assertEqual(self.chart(root, "sz300209", "day", "qfq")["meta"]["token"], before["meta"]["token"])

        changed = root / "changed.csv"
        changed.write_text(HEADER + "\n" + GOOD_DAY.replace(",4.46,358454", ",4.47,358454") + "\n")
        report = ingest.import_file(changed, target)
        self.assertEqual(report["codes"]["sz300209"]["day"]["pending_review"], 1)
        after = self.chart(root, "sz300209", "day", "raw")
        self.assertEqual(next(b for b in after["kline"] if b["time"] == "2024-10-08")["close"], 4.46)
        pending = target.conn().execute("SELECT code, dataset, key FROM pending_review WHERE verdict IS NULL")
        self.assertEqual([tuple(r) for r in pending], [("sz300209", "day", "2024-10-08")])


class HongKongImport(_IngestCase):
    def test_raw_is_served_and_vendor_qfq_is_not_faked(self):
        root = self.fresh_dir()
        path = root / "hk.csv"
        path.write_text("\n".join((HEADER,
                                   "hk00700,day,2026-09-24,600,610,598,605,1000000,share,,598,0",
                                   "hk00700,day,2026-09-25,605,606,590,592,1200000,share,,605,0")) + "\n")
        ingest.import_file(path, self.worker(root))
        out = self.chart(root, "hk00700", "day", "raw")
        self.assertEqual([(b["time"], b["close"], b["volume"]) for b in out["kline"]],
                         [("2026-09-24", 605.0, 1000000.0), ("2026-09-25", 592.0, 1200000.0)])
        cache_support.isolate_cache_dir(self, root)
        from chanapp.api.main import app
        r = TestClient(app).get("/api/chart?code=hk00700&freq=day&adjust=qfq")
        self.assertEqual(r.status_code, 502, r.text[:300])     # 前复权要供应商缓存，导入不提供


class CommandLine(_IngestCase):
    def run_cli(self, path):
        root = self.fresh_dir()
        cache_support.isolate_cache_dir(self, root)
        self.enterContext(mock.patch.dict("os.environ", {"CHANAPP_INSTANCE_CONFIG": ""}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ingest.main([str(path)])
        return code, json.loads(out.getvalue()), root

    def test_example_file_succeeds(self):
        code, out, root = self.run_cli(EXAMPLE)
        self.assertEqual((code, out["status"], out["rows"]), (0, "ok", 193))
        self.assertEqual(out["codes"]["sh600036"]["m15"]["inserted"], 16)
        self.assertEqual(out["codes"]["sz300209"]["day"]["inserted"], 79)
        self.assertEqual(len(self.chart(root, "sh000001", "day", "raw")["kline"]), 98)

    def test_rejected_example_reports_problems_and_writes_nothing(self):
        code, out, root = self.run_cli(FIX / "ingest" / "cn-rejected-example.csv")
        self.assertEqual((code, out["status"]), (1, "rejected"))
        self.assertEqual([(p["line"], p["reason"]) for p in out["problems"]],
                         [(3, admission.BAD_ENUM), (4, admission.BAD_OHLC), (6, admission.OFF_GRID)])
        conn = facts.open_facts(root / facts.DB_NAME)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 0)

    def test_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(ingest.main([]), 2)
        self.assertIn("用法", err.getvalue())


if __name__ == "__main__":
    unittest.main()
