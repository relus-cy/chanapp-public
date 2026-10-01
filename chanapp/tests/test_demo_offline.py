"""默认演示模式的 HTTP 端到端验收（真实 lifespan、正式 CSV 导入、仅拦外部边界）。

失败方式（先列后测）：
- 环境残留非空凭据使默认 demo 开始出站，或 HTTP C 扩展绕过 socket 拦截；
- 启动、主图、分页、手动重拉、未知代码触发补取或尝试事实写锁；
- 事实库被计算审计或读取路径改写，历史计算混入 online_observed；
- 搜索回退联网、报价使用在线源，F10 仍展示联网信息；
- 历史数据被标 stale，截止日期与对应周期末根数据不一致；
- AI 有凭据即启用，session 仍宣告市场开放；
- 显式采集开关覆盖 demo、空库普通路由建立事实库、实例个人状态写错位置；
- 历史事实不变却因墙钟跨日改变 token、K线或 forming；
- 两个首开图表并发初始化审计库，重复 DDL 被发布路径吞错，HTTP 成功却缺少审计；
- 样本截止之后、日历里仍有的交易日被当作分钟缺根；
- demo 跳过周期偏好初始化（/api/periods 503、图表被页面挡住），或全新 demo 弹首次提示、预置覆盖个人选择。
未覆盖：第三方 SDK 在独立子进程直接发起系统调用（本应用请求链不启动此类进程）。
"""
import csv
import fcntl
import hashlib
import os
import socket
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.api.main import app
from chanapp.engine import data as engine_data
from chanapp.engine.kline import collector, facts, ingest
from chanapp.engine.kline.providers.registry import REGISTRY

EXAMPLE = Path(__file__).parent / "fixtures" / "ingest" / "cn-example.csv"


class DemoOfflineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        excluded = {"CHANAPP_INSTANCE_CONFIG", "CHANAPP_CACHE_DIR", "WATCHLIST_PATH", "VIEW_LOG_PATH",
                    "ANALYSIS_CACHE_DIR"}
        env = {key: value for key, value in os.environ.items() if key not in excluded}
        env.update({key: "offline-test-placeholder" for spec in REGISTRY.values() for key in spec.credentials})
        env.update(COLLECTOR_ENABLED="0", CHANAPP_CACHE_DIR=str(self.root),
                   WATCHLIST_PATH=str(self.root / "watchlist.json"), LLM_API_KEY="offline-test-placeholder")
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        self.enterContext(mock.patch.object(engine_data, "CACHE_DIR", self.root))
        (self.root / "watchlist.json").write_text(
            '[{"code":"sz300209","name":"示例证券"},{"code":"hk00700","name":"港股示例"}]')
        worker = collector.Collector(self.root, providers={}, clock=lambda: datetime(2030, 1, 1).timestamp())
        result = ingest.import_file(EXAMPLE, worker)
        self.assertEqual(result["rows"], 193)
        # CSV 契约没有名称；为本地证券目录另提供测试名称，仍经唯一写者锁准备事实夹具。
        with worker.writer() as conn:
            conn.execute("INSERT INTO instruments(code, name, kind, source, fetched_at) "
                         "VALUES (?, ?, ?, ?, ?)",
                         ("sz300209", "示例证券", "stock", "import", "2030-01-01T00:00:00"))
        worker.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        worker.conn().close()
        self.fact_path = self.root / facts.DB_NAME
        self.before = hashlib.sha256(self.fact_path.read_bytes()).hexdigest()
        self.wal_path = Path(str(self.fact_path) + "-wal")
        self.wal_before = self.wal_path.read_bytes() if self.wal_path.exists() else b""
        with EXAMPLE.open(newline="") as stream:
            self.rows = list(csv.DictReader(stream))
        self.outbound = []
        self.writer_attempts = []

        def deny(*args, **kwargs):
            self.outbound.append("outbound transport attempted")
            raise OSError("offline acceptance: outbound transport forbidden")

        for name in ("connect", "connect_ex"):
            self.enterContext(mock.patch.object(socket.socket, name, side_effect=deny))
        for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
            self.enterContext(mock.patch.object(socket, name, side_effect=deny))
        # C HTTP 传输不经过 Python socket；在它实际执行请求的边界拦截。
        try:
            from curl_cffi import Curl
        except ImportError:
            pass
        else:
            self.enterContext(mock.patch.object(Curl, "perform", side_effect=deny))
        real_flock = fcntl.flock

        def watch_lock(fd, operation):
            if operation & fcntl.LOCK_EX:
                self.writer_attempts.append(operation)
            return real_flock(fd, operation)

        self.enterContext(mock.patch.object(fcntl, "flock", side_effect=watch_lock))
        # 退出生命周期之后验证启动/关闭阶段也没有隐蔽出站或事实写入。
        self.addCleanup(self.assert_boundaries)
        self.client = self.enterContext(TestClient(app))

    def assert_boundaries(self):
        with self.subTest(boundary="network"):
            self.assertEqual(self.outbound, [], "默认 demo 不应尝试出站，包括被捕获并降级的调用")
        with self.subTest(boundary="writer_lock"):
            self.assertEqual(self.writer_attempts, [], "默认 demo 不应尝试事实写锁")
        with self.subTest(boundary="fact_bytes"):
            self.assertEqual(hashlib.sha256(self.fact_path.read_bytes()).hexdigest(), self.before)
            self.assertEqual(self.wal_path.read_bytes() if self.wal_path.exists() else b"", self.wal_before)
        with self.subTest(boundary="fact_audit"):
            with sqlite3.connect(f"file:{self.fact_path}?mode=ro", uri=True) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM calc_runs").fetchone()[0], 0)

    def get(self, path, **params):
        response = self.client.get(path, params=params)
        self.assertEqual(response.status_code, 200, response.text[:500])
        return response.json()

    def test_chart_history_refetch_and_unknown_code_never_fetch_or_write(self):
        chart = self.get("/api/chart", code="sz300209", freq="day", adjust="raw")
        self.assertGreater(chart["meta"]["bars"], 0)
        self.get("/api/chart", code="sz300209", freq="day", adjust="raw", before="2024-12-01", limit=10,
                 token=chart["meta"]["token"])
        response = self.client.get("/api/chart", params={"code": "sz300209", "freq": "day",
                                                       "adjust": "raw", "refetch": "true"})
        self.assertEqual(response.status_code, 200, response.text[:500])
        self.assertEqual(response.headers.get("X-Refetch-Status"), "disabled")
        missing = self.client.get("/api/chart", params={"code": "sz999999", "freq": "day"})
        self.assertIn(missing.status_code, (200, 502))
        if missing.status_code == 200:
            self.assertEqual(missing.json()["meta"]["bars"], 0)

    def test_each_period_has_historical_cutoff_without_stale(self):
        for code, freq in (("sz300209", "day"), ("sh600036", "m30"), ("sh600036", "m60")):
            with self.subTest(code=code, freq=freq):
                body = self.get("/api/chart", code=code, freq=freq, adjust="raw")
                meta = body["meta"]
                self.assertFalse(meta["stale"])
                self.assertIsNone(meta["stale_age_s"])
                self.assertEqual(meta["coverage"]["data_status"],
                                 {"phase": "historical", "day": meta["last_dt"][:10], "at": None})

    def test_minute_quality_marks_stop_at_sample_cutoff(self):
        # 指数日线比个股分钟多出几个交易日：个股截止之后的日历日不是该样本的缺根
        self.client.__exit__(None, None, None)
        import json
        root = self.root / "cutoff"
        slots = ["09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30",
                 "13:15", "13:30", "13:45", "14:00", "14:15", "14:30", "14:45", "15:00"]
        lines = ["code,freq,dt,open,high,low,close,volume,volume_unit,amount,pc,suspended"]
        lines += [f"{r['code']},{r['freq']},{r['dt']},{r['open']},{r['high']},{r['low']},{r['close']},"
                  f"{r['volume']},{r['volume_unit']},{r['amount']},{r['pc']},{r['suspended']}"
                  for r in self.rows if r["code"] == "sh000001" and "2024-10-08" <= r["dt"] <= "2024-10-16"]
        lines += [f"sh600036,m15,2024-10-0{d} {t},40.5,40.6,40.4,40.5,1000.0,lot,4050000.0,,0"
                  for d in (8, 9) for t in slots]
        lines += [f"sh600036,day,2024-10-0{d},40.5,40.6,40.4,40.5,16000.0,lot,64800000.0,40.5,0" for d in (8, 9)]
        sample = self.root / "cutoff.csv"
        sample.write_text("\n".join(lines) + "\n")
        worker = collector.Collector(root / "data", providers={}, clock=lambda: datetime(2030, 1, 1).timestamp())
        ingest.import_file(sample, worker)
        worker.conn().close()
        self.writer_attempts.clear()            # 夹具准备经唯一写者导入；之后的应用阶段仍不得申请写锁
        config = self.root / "cutoff.json"
        config.write_text(json.dumps({"mode": "demo", "instance_dir": str(root)}))
        env = {key: value for key, value in os.environ.items()
               if key not in ("CHANAPP_CACHE_DIR", "WATCHLIST_PATH")}
        env.update(CHANAPP_INSTANCE_CONFIG=str(config))
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(engine_data, "CACHE_DIR", root / "data"), TestClient(app) as client:
            response = client.get("/api/chart", params={"code": "sh600036", "freq": "m30", "adjust": "raw"})
            self.assertEqual(response.status_code, 200, response.text[:300])
            meta = response.json()["meta"]
        self.assertEqual(meta["coverage"]["data_status"]["day"], "2024-10-09")
        self.assertEqual(meta["incomplete_days"], [])

    def test_search_is_local(self):
        found = self.get("/api/search", q="300209")
        self.assertIn("sz300209", [item["code"] for item in found])
        self.assertEqual(self.get("/api/search", q="unlisted-offline-security"), [])
        self.assertIn("sz300209", [item["code"] for item in self.get("/api/search", q="示例证券")])
        self.assertIn("sh000001", [item["code"] for item in self.get("/api/search", q="000001")])

    def test_quotes_are_local(self):
        expected = [row for row in self.rows if row["code"] == "sz300209" and row["freq"] == "day"][-1]
        quote = self.get("/api/quote", code="sz300209")["quote"]
        self.assertEqual(quote["price"], float(expected["close"]))
        self.assertEqual(self.get("/api/quotes")["quotes"]["sz300209"]["price"], float(expected["close"]))
        self.get("/api/quote", code="hk00700")

    def test_f10_has_explicit_empty_state(self):
        detail = self.get("/api/f10", code="sz300209")
        self.assertFalse(detail["f10"])
        self.assertFalse(detail["flow"])
        self.assertIsNone(detail["industry_pct"])

    def test_session_is_closed(self):
        body = self.get("/api/session")
        self.assertEqual(body.get("mode"), "demo")
        self.assertEqual(body["markets"], {"cn": {"open": False}, "hk": {"open": False}})

    def test_ai_disabled_despite_credentials(self):
        self.assertEqual(self.get("/api/analysis", code="sz300209", freq="day")["status"], "disabled")

    def test_calculation_audit_is_separate_historical_recompute(self):
        self.get("/api/chart", code="sz300209", freq="day", adjust="raw")
        audit = self.root / "demo-audit.sqlite"
        self.assertTrue(audit.is_file(), "历史计算审计应写入独立 demo-audit.sqlite")
        with sqlite3.connect(f"file:{audit}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT code, freq, source_kind, input_end FROM calc_runs").fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row[2] == "historical_recompute" for row in rows), rows)
        self.assertIn(("sz300209", "day"), [(row[0], row[1]) for row in rows])

    def test_collector_environment_cannot_enable_default_demo(self):
        self.client.__exit__(None, None, None)
        with mock.patch.dict(os.environ, {"COLLECTOR_ENABLED": "1"}):
            with TestClient(app) as client:
                status = client.get("/api/status")
                self.assertEqual(status.status_code, 200)
                self.assertFalse(status.json()["enabled"])
                response = client.get("/api/chart", params={"code": "sz300209", "freq": "day",
                                                           "adjust": "raw", "refetch": "true"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["X-Refetch-Status"], "disabled")

    def test_empty_demo_ordinary_routes_do_not_create_facts(self):
        self.client.__exit__(None, None, None)
        empty = self.root / "empty"
        # 门面的缓存根在 setUp 已固定为样本目录，这里一并切换，否则读到的仍是样本
        with mock.patch.dict(os.environ, {"CHANAPP_CACHE_DIR": str(empty)}), \
                mock.patch.object(engine_data, "CACHE_DIR", empty):
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/search", params={"q": "300209"}).json(), [])
                for path, params in (("/api/session", {}), ("/api/status", {}),
                                     ("/api/search", {"q": "300209"}), ("/api/quotes", {}),
                                     ("/api/quote", {"code": "hk00700"}),
                                     ("/api/f10", {"code": "sz300209"}),
                                     ("/api/analysis", {"code": "sz300209"})):
                    with self.subTest(path=path):
                        response = client.get(path, params=params)
                        self.assertEqual(response.status_code, 200, response.text[:300])
                missing = client.get("/api/chart", params={"code": "sz300209"})
                self.assertIn(missing.status_code, (200, 502))
        self.assertFalse((empty / facts.DB_NAME).exists())
        self.assertFalse((empty / facts.WRITER_LOCK_NAME).exists())

    def test_explicit_demo_instance_directory_keeps_personal_workflow_local(self):
        self.client.__exit__(None, None, None)
        import json
        config = self.root / "demo.json"
        config.write_text(json.dumps({"mode": "demo", "instance_dir": "personal"}))
        env = {key: value for key, value in os.environ.items()
               if key not in ("CHANAPP_CACHE_DIR", "WATCHLIST_PATH")}
        env.update(CHANAPP_INSTANCE_CONFIG=str(config), COLLECTOR_ENABLED="1")
        with mock.patch.dict(os.environ, env, clear=True):
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/session").json()["mode"], "demo")
                code = "sh000001"
                client.delete(f"/api/watchlist/{code}")
                response = client.post("/api/watchlist", json={"code": code, "name": "本地指数"})
                self.assertEqual(response.status_code, 200)
                response = client.put(f"/api/watchlist/{code}/tags", json={"tags": ["演示"]})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(next(row for row in response.json() if row["code"] == code)["tags"], ["演示"])
                response = client.post("/api/views", json={"code": code, "name": "本地指数", "freq": "week"})
                self.assertEqual(response.status_code, 200)
                recent = client.get("/api/views").json()["recent"]
                self.assertEqual(recent[0]["code"], code)
                self.assertTrue(recent[0]["watched"])
                self.assertEqual(client.delete(f"/api/watchlist/{code}").status_code, 200)
                self.assertFalse(client.get("/api/views").json()["recent"][0]["watched"])
        personal = self.root / "personal"
        self.assertTrue((personal / "watchlist.json").is_file())
        self.assertTrue((personal / "views.sqlite").is_file())
        self.assertFalse((personal / "data" / facts.DB_NAME).exists())

    def test_fresh_demo_presets_four_periods_without_prompt_and_keeps_choice(self):
        self.client.__exit__(None, None, None)
        import json
        config = self.root / "demo.json"
        config.write_text(json.dumps({"mode": "demo", "instance_dir": "fresh"}))
        env = {key: value for key, value in os.environ.items()
               if key not in ("CHANAPP_CACHE_DIR", "WATCHLIST_PATH")}
        env.update(CHANAPP_INSTANCE_CONFIG=str(config))
        with mock.patch.dict(os.environ, env, clear=True):
            with TestClient(app) as client:
                response = client.get("/api/periods")
                self.assertEqual(response.status_code, 200, response.text[:300])
                state = response.json()
                self.assertEqual(state["selected"], ["day", "week", "m60", "m30"])
                self.assertIsNone(state["notice"])
                saved = client.put("/api/periods", json={"selected": ["day"], "revision": state["revision"]})
                self.assertEqual(saved.status_code, 200, saved.text[:300])
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/periods").json()["selected"], ["day"])
        self.assertFalse((self.root / "fresh" / "data" / facts.DB_NAME).exists())

    def test_historical_views_stay_stable_across_clock_days(self):
        from chanapp.engine.kline import views

        class WallClock(datetime):
            instant = datetime(2025, 1, 27, 10)

            @classmethod
            def now(cls, tz=None):
                return cls.instant if tz is None else cls.instant.replace(tzinfo=tz)

        for code, freq, adjust in (("sz300209", "day", "qfq"), ("sz300209", "week", "raw"),
                                   ("sh600036", "m30", "raw")):
            with self.subTest(code=code, freq=freq, adjust=adjust):
                outputs = []
                with mock.patch.object(views, "datetime", WallClock):
                    for instant in (datetime(2025, 1, 27, 10), datetime(2026, 9, 29, 10),
                                    datetime(2030, 1, 1, 16)):
                        WallClock.instant = instant
                        body = self.get("/api/chart", code=code, freq=freq, adjust=adjust)
                        outputs.append((body["meta"]["token"], body["kline"]))
                        # HTTP 图表省略 bar.forming；用同一视图的只读入口补查，仍无业务替身。
                        conn = facts.open_readonly(self.fact_path)
                        try:
                            view = views.read_view(conn, code, freq, adjust=adjust)
                            self.assertTrue(all(not bar["forming"] for bar in view.bars))
                        finally:
                            conn.close()
                self.assertEqual(outputs[0], outputs[1])
                self.assertEqual(outputs[1], outputs[2])


    def test_concurrent_first_charts_preserve_every_calculation_audit(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        audit = self.root / "demo-audit.sqlite"
        self.assertFalse(audit.exists())
        old_version_reads = Barrier(2)
        real_connect = sqlite3.connect

        class AuditCursor(sqlite3.Cursor):
            checking_version = False

            def execute(self, sql, parameters=()):
                self.checking_version = sql.strip().upper() == "PRAGMA USER_VERSION"
                return super().execute(sql, parameters)

            def fetchone(self):
                row = super().fetchone()
                # 确定性复现两个连接均已读到旧版本，随后才竞争初始化。
                # 版本读取若已被写事务保护，第二连接应排队，不在这里等待以免死锁。
                if self.checking_version and row and row[0] == 0 and not self.connection.in_transaction:
                    old_version_reads.wait(timeout=5)
                return row

        class AuditConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                return self.cursor(factory=AuditCursor).execute(sql, parameters)

        def connect(database, *args, **kwargs):
            if str(database) == str(audit):
                kwargs["factory"] = AuditConnection
            return real_connect(database, *args, **kwargs)

        def request_chart(code):
            return self.client.get("/api/chart", params={"code": code, "freq": "week", "adjust": "raw"})

        # 两个主图均为 week，避免后续 day/m30/m60 共振的另一条记录偶然修复漏写。
        with mock.patch.object(sqlite3, "connect", side_effect=connect):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(request_chart, ("sh000001", "sz300209")))
        expected = set()
        for code, response in zip(("sh000001", "sz300209"), responses):
            self.assertEqual(response.status_code, 200, response.text[:300])
            body = response.json()
            expected.add((code, "week", body["meta"]["data_version"], body["calculation_id"]))
        with real_connect(f"file:{audit}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT code, freq, input_data_version, calculation_id FROM calc_runs "
                                "WHERE source_kind='historical_recompute' AND status IN ('ok', 'ok_no_signals')")
            recorded = set(rows.fetchall())
        self.assertTrue(expected <= recorded, f"HTTP成功但缺少计算审计: {expected - recorded}")
