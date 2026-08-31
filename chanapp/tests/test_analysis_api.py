"""Task 11: /api/analysis 完全分类生成（结构哈希缓存）测试。

全部 mock LLM / 冻结 fixture，离线可跑，不打外网：
- engine_data.get_bars 用 fixture 替换（sh000001 日线 800 根快照）；
- engine_llm.analyze 用 unittest.mock 替换；
- 缓存目录用 ANALYSIS_CACHE_DIR 注入临时目录（同 WATCHLIST_PATH 模式）。
真 key 冒烟（DEEPSEEK_API_KEY 到位后 curl /api/analysis 二次请求 cached:true）另行手动执行。
"""
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

FIXTURE = Path(__file__).parent / "fixtures" / "sh000001_day_qfq.csv"


def load_bars():
    with open(FIXTURE, encoding="utf-8") as f:
        return [
            {"dt": r["dt"], "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]),
             "volume": float(r["volume"])}
            for r in csv.DictReader(f)
        ]


class TestAnalysisApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.environ["ANALYSIS_CACHE_DIR"] = self._tmp.name
        self.addCleanup(os.environ.pop, "ANALYSIS_CACHE_DIR")
        # 保存并清空 LLM_* 环境变量，测试内按需设置
        self._saved = {k: os.environ.get(k)
                       for k in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL")}
        for k in self._saved:
            os.environ.pop(k, None)

        def _restore():
            for k, v in self._saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(_restore)
        from chanapp.api.main import app
        self.c = TestClient(app)

    def _dataset(self):
        return {"bars": load_bars(), "meta": {"source": "fixture"}}

    def test_unconfigured_returns_without_call(self):
        """无 LLM_API_KEY → status unconfigured，且不取数、不调 LLM。"""
        with mock.patch("chanapp.api.analysis.engine_data.get_bars",
                        side_effect=AssertionError("unconfigured 时不应取数")) as gb:
            r = self.c.get("/api/analysis?code=sh000001&freq=day")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "unconfigured")
        gb.assert_not_called()

    def test_ok_then_cached(self):
        """正常路径：解析 LLM JSON → 落缓存；二次请求命中缓存且不再调 LLM。"""
        os.environ["LLM_API_KEY"] = "k"
        payload = json.dumps({
            "current_state": "中枢下沿附近回落。当前后验排序：破位下行最占优。",
            "scenarios": [
                {"name": "反弹",
                 "prior": "先验：B1a 3741.11 以来上行结构未证伪",
                 "evidence_for": ["B1a 3741.11 依据卡，DIF 未创新低"],
                 "evidence_against": ["末bar 收 3905.2 已在中枢下沿 3922.58 之下"],
                 "posterior": "低：近端证据逆风，需先收复中枢下沿",
                 "trigger": "站上 3922.58", "boundary": "4025.7",
                 "action": "减仓", "basis": "B1a 3741.11 依据卡",
                 "update_watch": "若 S1-d 确认则本判断作废"},
            ],
        }, ensure_ascii=False)
        with mock.patch("chanapp.api.analysis.engine_data.get_bars",
                        return_value=self._dataset()), \
             mock.patch("chanapp.api.analysis.engine_llm.analyze",
                        return_value=payload) as an:
            r1 = self.c.get("/api/analysis?code=sh000001&freq=day")
            self.assertEqual(r1.status_code, 200)
            j1 = r1.json()
            self.assertEqual(j1["status"], "ok")
            self.assertFalse(j1["cached"])
            self.assertEqual(j1["ref_bar_dt"], load_bars()[-1]["dt"])  # 结构参考时点
            self.assertIn("后验排序", j1["current_state"])
            self.assertEqual(len(j1["scenarios"]), 1)
            sc = j1["scenarios"][0]
            self.assertEqual(sc["trigger"], "站上 3922.58")
            self.assertEqual(sc["prior"], "先验：B1a 3741.11 以来上行结构未证伪")
            self.assertEqual(sc["posterior"], "低：近端证据逆风，需先收复中枢下沿")
            self.assertEqual(sc["evidence_for"], ["B1a 3741.11 依据卡，DIF 未创新低"])
            self.assertEqual(sc["update_watch"], "若 S1-d 确认则本判断作废")
            self.assertTrue(j1["hash"])

            r2 = self.c.get("/api/analysis?code=sh000001&freq=day")
            j2 = r2.json()
            self.assertEqual(j2["status"], "ok")
            self.assertTrue(j2["cached"])
            self.assertEqual(j2["ref_bar_dt"], j1["ref_bar_dt"])  # 缓存路径同样带出
            self.assertEqual(j2["scenarios"], j1["scenarios"])
            self.assertEqual(j2["hash"], j1["hash"])
            self.assertEqual(an.call_count, 1)  # 第二次未调 LLM

        # 缓存文件按结构哈希命名落在注入目录
        files = list(Path(self._tmp.name).glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stem, j1["hash"])

    def test_markdown_fenced_json_parsed(self):
        """LLM 输出包 ```json 代码围栏时仍能解析。"""
        os.environ["LLM_API_KEY"] = "k"
        fenced = "```json\n{\"current_state\": \"s\", \"scenarios\": []}\n```"
        with mock.patch("chanapp.api.analysis.engine_data.get_bars",
                        return_value=self._dataset()), \
             mock.patch("chanapp.api.analysis.engine_llm.analyze",
                        return_value=fenced):
            j = self.c.get("/api/analysis?code=sh000001&freq=day").json()
        self.assertEqual(j["status"], "ok")
        self.assertEqual(j["current_state"], "s")
        self.assertNotIn("raw", j)

    def test_invalid_json_degrades_to_raw(self):
        """LLM 输出无法解析为 JSON → 降级返回 raw 文本，不报错。"""
        os.environ["LLM_API_KEY"] = "k"
        with mock.patch("chanapp.api.analysis.engine_data.get_bars",
                        return_value=self._dataset()), \
             mock.patch("chanapp.api.analysis.engine_llm.analyze",
                        return_value="这不是 JSON"):
            j = self.c.get("/api/analysis?code=sh000001&freq=day").json()
        self.assertEqual(j["status"], "ok")
        self.assertEqual(j["raw"], "这不是 JSON")
        self.assertEqual(j["scenarios"], [])

    def test_structure_hash_changes_with_bars(self):
        """缓存键随 code/freq/末bar dt/最新信号 dt 变化。"""
        from chanapp.api import analysis
        from chanapp.engine import signals, structure
        bars = load_bars()
        st = structure.compute_structure(bars, "sh000001", "day")
        sig = signals.compute_signals(bars, st)
        h1 = analysis._structure_hash("sh000001", "day", bars, sig)
        self.assertEqual(len(h1), 64)
        self.assertNotEqual(h1, analysis._structure_hash("sh000001", "m60", bars, sig))
        self.assertNotEqual(h1, analysis._structure_hash("sz399006", "day", bars, sig))
        sig2 = {"signals": [], "forming_signal": None}  # 信号集合变化 → 哈希变化
        self.assertNotEqual(h1, analysis._structure_hash("sh000001", "day", bars, sig2))

    def test_prompt_contains_only_traceable_numbers(self):
        """prompt 只含可追溯价位；数据段无 % 数字；指令段含禁止概率约束。

        计划原断言 `"概率" not in p` 与「prompt 必须写禁止输出概率的指令」矛盾
        （指令本身含「概率」二字），调整为：数据段（JSON）无 %，指令段含禁止约束。
        """
        from chanapp.api import analysis
        from chanapp.engine import evidence, signals, structure
        bars = load_bars()
        st = structure.compute_structure(bars, "sh000001", "day")
        sig = signals.compute_signals(bars, st)
        ev = evidence.build_evidence(sig["signals"], st)
        data = analysis.collect_prompt_data("sh000001", "day", bars, st, sig, ev)
        p = analysis.build_prompt(data)

        # 来自结构数据的可追溯价位（fixture 金标准：B1a 3741.11、中枢上沿/下沿、末bar）
        for token in ["3741.11", "4025.7", "3922.58", "3905.2", "2026-08-21"]:
            self.assertIn(token, p)
        # 中枢键名用白话术语，不出 ZG/ZD 缩写
        self.assertIn("中枢上沿", data["latest_zs"])
        self.assertIn("中枢下沿", data["latest_zs"])
        self.assertNotIn("ZG", p)
        self.assertNotIn("ZD", p)
        # 依据卡最多 5 张
        self.assertLessEqual(len(data["recent_evidence"]), 5)

        data_part = p.split("数据（JSON）：", 1)[1].split("输出要求：", 1)[0]
        self.assertNotIn("%", data_part)       # 衰竭度等百分数不喂给 LLM
        self.assertNotIn("衰竭度", data_part)
        rules = p.split("输出要求：", 1)[1]
        self.assertIn("概率", rules)           # 禁止概率指令
        self.assertIn("价位", rules)           # trigger/boundary 必须引用输入价位

    def test_prompt_contains_bayesian_contract(self):
        """prompt 指令段含贝叶斯契约：prior/posterior/update_watch 字段、
        证据必须引用输入信号 label 与价位、后验定性三档、current_state 含后验排序。"""
        from chanapp.api import analysis
        from chanapp.engine import evidence, signals, structure
        bars = load_bars()
        st = structure.compute_structure(bars, "sh000001", "day")
        sig = signals.compute_signals(bars, st)
        ev = evidence.build_evidence(sig["signals"], st)
        data = analysis.collect_prompt_data("sh000001", "day", bars, st, sig, ev)
        p = analysis.build_prompt(data)
        rules = p.split("输出要求：", 1)[1]

        for field in ["prior", "evidence_for", "evidence_against",
                      "posterior", "update_watch"]:
            self.assertIn(field, rules)
        self.assertIn("先验", rules)
        self.assertIn("后验", rules)
        self.assertIn("后验排序", rules)       # current_state 必含当前后验排序
        self.assertIn("高", rules)             # 后验定性三档
        self.assertIn("中", rules)
        self.assertIn("低", rules)
        self.assertIn("label", rules)          # 证据必须引用输入信号 label
        self.assertIn("中枢上沿", rules)       # 证据必须引用输入中枢（白话术语）
        self.assertIn("中枢下沿", rules)
        self.assertIn("MM-DD", rules)          # 输出日期格式约束

    def test_parse_tolerates_legacy_scenario_fields(self):
        """旧格式（无贝叶斯字段）仍能解析，字段缺失不补；evidence 字符串规整为列表。"""
        from chanapp.api import analysis
        legacy = json.dumps({
            "current_state": "回落",
            "scenarios": [
                {"name": "反弹", "trigger": "站上 3922.58", "boundary": "4025.7",
                 "action": "减仓", "basis": "B1a 3741.11 依据卡"},
                {"name": "破位", "evidence_for": "末bar 收 3905.2 破中枢下沿",
                 "evidence_against": ["B1a 3741.11 未破", 42]},
            ],
        }, ensure_ascii=False)
        parsed = analysis._parse_llm_output(legacy)
        self.assertIsNotNone(parsed)
        s0, s1 = parsed["scenarios"]
        self.assertNotIn("prior", s0)          # 旧格式缺失字段不补齐
        self.assertEqual(s0["trigger"], "站上 3922.58")
        self.assertEqual(s1["evidence_for"], ["末bar 收 3905.2 破中枢下沿"])  # str → list
        self.assertEqual(s1["evidence_against"], ["B1a 3741.11 未破"])   # 非字符串剔除

    def test_prompt_includes_forming_signal(self):
        """forming 信号存在时进入 prompt 数据。"""
        from chanapp.api import analysis
        bars = load_bars()
        forming = {"dt": "2026-08-21", "price": 3905.2, "side": "sell",
                   "label": "~S1-d", "cond": "dif", "forming": True,
                   "anchor": {"dt": "2026-07-20", "price": 3741.11},
                   "area_pair": [1.0, 2.0], "dif_pair": [0.1, 0.2], "note": "雏形"}
        data = analysis.collect_prompt_data(
            "sh000001", "day", bars, {"zs": []},
            {"signals": [], "forming_signal": forming}, [])
        p = analysis.build_prompt(data)
        self.assertIn("~S1-d", p)


if __name__ == "__main__":
    unittest.main()
