"""#39 实例目录与个人状态隔离：经应用 lifespan 与 HTTP 接口端到端验证（录制输入，不访问网络）。

失败方式（先列后测）：
- 设了实例目录，事实库（含计算审计）、显示缓存、AI 缓存仍落在仓库 `.cache`；
- 自选写回仓库种子，或个人修改改动种子；
- 重复初始化覆盖已有自选或查看记录，或把种子的后续变化再次灌入；
- 两个实例共用任一状态文件（进程内按路径缓存的连接、采集器、查看记录对象串用）；
- 显式环境变量被实例目录派生路径覆盖，作者生产实例（不写 instance_dir）换了读写文件或被自动搬迁；
- 未设 WATCHLIST_PATH 也未设实例目录时仍写仓库种子；
- 生命周期退出后缓存根与实例路径没有恢复，后续库调用写错库；
- 实例目录不可用（指向文件）时静默降级到别处。
- 新建的实例根目录与查看记录按 umask 放开给本机其他用户读（应为 0700 / 0600）；已有目录被改权限。
未覆盖：种子复制中途崩溃（复制先写临时文件再原子链接，未做故障注入）；内部自检脚本仍只读环境变量。
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chanapp.api import analysis as api_analysis
from chanapp.api.main import app
from chanapp.engine import data as engine_data
from chanapp.engine import instance_paths

PKG_ROOT = Path(__file__).resolve().parent.parent
_PATH_VARS = ("CHANAPP_INSTANCE_CONFIG", "CHANAPP_CACHE_DIR", "WATCHLIST_PATH", "VIEW_LOG_PATH",
              "ANALYSIS_CACHE_DIR")


def _repo_snapshot() -> dict:
    """仓库工作树快照（含被忽略文件，不含 .git 与字节码）：路径 → (大小, 修改时间)。"""
    out = {}
    for base, dirs, files in os.walk(PKG_ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for name in files:
            if name == ".git":
                continue
            path = Path(base) / name
            st = path.lstat()
            out[str(path.relative_to(PKG_ROOT))] = (st.st_size, st.st_mtime_ns)
    return out


class InstanceDirTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        env = {k: v for k, v in os.environ.items() if k not in _PATH_VARS}
        env["COLLECTOR_ENABLED"] = "0"
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        # 用原值打补丁：只为退出时恢复门面缓存根，不改变本例看到的缺省
        restore = mock.patch.object(engine_data, "CACHE_DIR", engine_data.CACHE_DIR)
        restore.start()
        self.addCleanup(restore.stop)
        self.seed = self.tmp / "seed.json"
        self.seed.write_text(json.dumps([{"code": "sh600519", "name": "贵州茅台"}], ensure_ascii=False))
        seed_patch = mock.patch.object(instance_paths, "WATCHLIST_SEED", self.seed)
        seed_patch.start()
        self.addCleanup(seed_patch.stop)

    def config(self, body, name="instance.json") -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(body))
        os.environ["CHANAPP_INSTANCE_CONFIG"] = str(path)
        return path

    def codes(self, client):
        response = client.get("/api/watchlist")
        self.assertEqual(response.status_code, 200, response.text)
        return [w["code"] for w in response.json()]

    def add(self, client, code, name):
        response = client.post("/api/watchlist", json={"code": code, "name": name})
        self.assertEqual(response.status_code, 200, response.text)

    def view(self, client, code, name):
        response = client.post("/api/views", json={"code": code, "name": name})
        self.assertEqual(response.status_code, 200, response.text)

    def recent(self, client):
        return [r["code"] for r in client.get("/api/views").json()["recent"]]

    def test_empty_instance_dir_holds_all_state_and_repo_stays_unchanged(self):
        with mock.patch.object(instance_paths, "WATCHLIST_SEED", PKG_ROOT / "watchlist.json"):
            seed = PKG_ROOT / "watchlist.json"
            expected = [w["code"] for w in json.loads(seed.read_text(encoding="utf-8"))] if seed.exists() else []
            before = _repo_snapshot()
            self.config({"instance_dir": "inst"})          # 相对路径以配置文件目录为基准
            root = self.tmp / "inst"
            with TestClient(app) as client:
                self.assertEqual(self.codes(client), expected)          # 种子只读复制
                self.add(client, "sh600036", "招商银行")
                self.view(client, "sh600036", "招商银行")
                engine_data.record_calc_run("sh600036", "day", input_start="2026-01-05",
                                            input_end="2026-09-30", input_data_version="v",
                                            calculation_id="c", signals=[])
                from chanapp.engine import display_feed
                paths = instance_paths.current()
                self.assertEqual(Path(engine_data.CACHE_DIR), root / "data")
                self.assertEqual(display_feed._cache_dir(), root / "data" / "display")
                self.assertEqual(api_analysis._cache_dir(), root / "data" / "analysis")
                self.assertEqual(paths.period_prefs, root / "periods.json")
            after = _repo_snapshot()
        self.assertEqual(before, after)
        saved = json.loads((root / "watchlist.json").read_text(encoding="utf-8"))
        self.assertEqual([w["code"] for w in saved], expected + ["sh600036"])
        self.assertTrue((root / "views.sqlite").exists())
        with sqlite3.connect(root / "data" / "demo-audit.sqlite") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM calc_runs").fetchone()[0], 1)
        # 生命周期结束即恢复：后续库调用不再写进这个实例
        self.assertNotEqual(Path(engine_data.CACHE_DIR), root / "data")
        self.assertIsNone(instance_paths.current().instance_dir)

    def test_reinitialize_keeps_existing_state_and_ignores_later_seed_changes(self):
        self.config({"instance_dir": str(self.tmp / "inst")})
        with TestClient(app) as client:
            self.add(client, "sz300308", "中际旭创")
            client.delete("/api/watchlist/sh600519")
            self.view(client, "sz300308", "中际旭创")
        self.seed.write_text(json.dumps([{"code": "hk00700", "name": "腾讯控股"}], ensure_ascii=False))
        for _ in range(2):                                   # 重复初始化两次
            with TestClient(app) as client:
                self.assertEqual(self.codes(client), ["sz300308"])
                self.assertEqual(self.recent(client), ["sz300308"])
        self.assertEqual(json.loads(self.seed.read_text(encoding="utf-8"))[0]["code"], "hk00700")

    def test_two_instances_do_not_share_state(self):
        first = self.config({"instance_dir": "a"}, "a.json")
        with TestClient(app) as client:
            self.add(client, "sz300308", "中际旭创")
            self.view(client, "sz300308", "中际旭创")
            engine_data.record_calc_run("sz300308", "day", input_start="2026-01-05", input_end="2026-09-30",
                                        input_data_version="a", calculation_id="c", signals=[])
        second = self.config({"instance_dir": "b"}, "b.json")
        with TestClient(app) as client:
            self.assertEqual(self.codes(client), ["sh600519"])        # 只有种子，没有 a 的修改
            self.assertEqual(self.recent(client), [])
            self.add(client, "hk00700", "腾讯控股")
            self.view(client, "hk00700", "腾讯控股")
        os.environ["CHANAPP_INSTANCE_CONFIG"] = str(first)
        with TestClient(app) as client:
            self.assertEqual(self.codes(client), ["sh600519", "sz300308"])
            self.assertEqual(self.recent(client), ["sz300308"])
        os.environ["CHANAPP_INSTANCE_CONFIG"] = str(second)
        with TestClient(app) as client:
            self.assertEqual(self.codes(client), ["sh600519", "hk00700"])
            self.assertEqual(self.recent(client), ["hk00700"])
        counts = {}
        for name in ("a", "b"):
            audit = self.tmp / name / "data" / "demo-audit.sqlite"
            counts[name] = 0
            if audit.exists():
                with sqlite3.connect(audit) as conn:
                    counts[name] = conn.execute("SELECT count(*) FROM calc_runs").fetchone()[0]
        self.assertEqual(counts, {"a": 1, "b": 0})

    def test_explicit_variables_win_over_instance_dir(self):
        explicit = self.tmp / "explicit"
        for name, value in (("CHANAPP_CACHE_DIR", explicit / "cache"), ("WATCHLIST_PATH", explicit / "w.json"),
                            ("VIEW_LOG_PATH", explicit / "v.sqlite"), ("ANALYSIS_CACHE_DIR", explicit / "ai")):
            os.environ[name] = str(value)
        self.config({"instance_dir": "inst"})
        with TestClient(app) as client:
            self.add(client, "sz300308", "中际旭创")
            self.view(client, "sz300308", "中际旭创")
            self.assertEqual(Path(engine_data.CACHE_DIR), explicit / "cache")
            self.assertEqual(api_analysis._cache_dir(), explicit / "ai")
            self.assertEqual(instance_paths.current().period_prefs, explicit / "w.periods.json")
        self.assertTrue((explicit / "w.json").exists())
        self.assertTrue((explicit / "v.sqlite").exists())
        self.assertFalse((explicit / "cache" / "facts.sqlite").exists(), "空 demo 不创建事实库")
        inst = self.tmp / "inst"
        self.assertFalse((inst / "watchlist.json").exists())
        self.assertFalse((inst / "views.sqlite").exists())
        self.assertFalse((inst / "data" / "facts.sqlite").exists())

    def test_author_production_instance_keeps_reading_and_writing_the_same_files(self):
        # 生产 unit 的环境变量（凭据值为占位，名字取自注册表）+ runbook 里的作者实例配置（不写 instance_dir）
        from chanapp.engine.kline.providers.catalog import REGISTRY
        app_dir, parent = self.tmp / "app", self.tmp
        watchlist = parent / "chanapp.watchlist.json"
        watchlist.write_text(json.dumps([{"code": "sz300308", "name": "中际旭创", "starred": True,
                                          "tags": ["光模块"]}], ensure_ascii=False))
        os.environ.update({"CHANAPP_CACHE_DIR": str(app_dir / ".cache"), "WATCHLIST_PATH": str(watchlist)})
        os.environ.update({key: "placeholder" for spec in REGISTRY.values() for key in spec.credentials})
        engine_data.CACHE_DIR = instance_paths.resolve().cache_dir   # 等同进程启动时导入门面
        self.config({"mode": "real",
                     "markets": {"CN": {"source": "mairui", "minute_fact_freq": "m15"},
                                 "HK": {"source": "longbridge", "minute_fact_freq": "m30"}},
                     "quota": {"per_minute": 300, "per_day": 10000}}, "author.json")
        before = {p for p in self.tmp.rglob("*") if p.is_file()}
        with TestClient(app) as client:
            self.assertEqual(self.codes(client), ["sz300308"])
            self.add(client, "sh600519", "贵州茅台")
            self.view(client, "sh600519", "贵州茅台")
            self.assertEqual(Path(engine_data.CACHE_DIR), app_dir / ".cache")
            self.assertEqual(engine_data._collector().cache_dir, app_dir / ".cache")  # 写者锁与日历导出所在
            self.assertTrue((app_dir / ".cache" / "facts.writer.lock").exists())
            self.assertEqual(api_analysis._cache_dir(), PKG_ROOT / ".cache" / "analysis")
            self.assertEqual(instance_paths.current().period_prefs, parent / "chanapp.watchlist.periods.json")
        saved = json.loads(watchlist.read_text(encoding="utf-8"))
        self.assertEqual([(w["code"], w["starred"], w["tags"]) for w in saved],
                         [("sz300308", True, ["光模块"]), ("sh600519", False, [])])
        created = {p for p in self.tmp.rglob("*") if p.is_file()} - before
        views = parent / "chanapp.watchlist.views.sqlite"
        self.assertIn(views, created)
        periods = parent / "chanapp.watchlist.periods.json"
        self.assertIn(periods, created)
        # 原有路径不搬迁：新增周期偏好与查看记录都随显式自选路径。
        self.assertEqual({p for p in created if p not in (views, periods) and app_dir / ".cache" not in p.parents}, set())
        self.assertEqual(self.seed.read_text(encoding="utf-8"),
                         json.dumps([{"code": "sh600519", "name": "贵州茅台"}], ensure_ascii=False))

    def test_without_instance_dir_or_watchlist_path_the_seed_is_never_written(self):
        legacy = self.tmp / "legacy-cache"
        engine_data.CACHE_DIR = legacy
        seed_before = self.seed.read_bytes()
        with TestClient(app) as client:                       # 无实例配置：demo
            self.assertEqual(self.codes(client), ["sh600519"])
            self.add(client, "sz300308", "中际旭创")
            self.view(client, "sz300308", "中际旭创")
            self.assertEqual(self.codes(client), ["sh600519", "sz300308"])
        self.assertEqual(self.seed.read_bytes(), seed_before)
        saved = json.loads((legacy / "watchlist.json").read_text(encoding="utf-8"))
        self.assertEqual([w["code"] for w in saved], ["sh600519", "sz300308"])
        self.assertTrue((legacy / "views.sqlite").exists())    # 查看记录位置保持原规则

    def test_new_instance_dir_and_view_log_are_private_existing_dirs_untouched(self):
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        self.config({"instance_dir": "inst"})
        shared = self.tmp / "shared"
        shared.mkdir(mode=0o755)
        os.environ["ANALYSIS_CACHE_DIR"] = str(shared)        # 已有的显式目录：不改权限
        with TestClient(app) as client:
            self.view(client, "sz300308", "中际旭创")
        root = self.tmp / "inst"
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((root / "views.sqlite").stat().st_mode & 0o777, 0o600)
        self.assertEqual(shared.stat().st_mode & 0o777, 0o755)

    def test_unusable_instance_dir_blocks_startup(self):
        (self.tmp / "occupied").write_text("not a directory")
        self.config({"instance_dir": "occupied"})
        with self.assertRaisesRegex(ValueError, "instance_dir"):
            with TestClient(app):
                pass
        self.assertIsNone(instance_paths.current().instance_dir)


if __name__ == "__main__":
    unittest.main()
