"""段级（线段级）信号金标准回归（离线 fixture 驱动，不打外网）。

金标准数值来自 tmp/chan-validation/chanlun_*_m30_beichi_summary.json 的
xd_strict_json_only 段（已验证事实）：xd 当 bi 走 v2 背驰判定（无上级线段，
锚点即相邻同向线段），label 加 "段:" 前缀。

structure 输出新增 zs_xd（观察者「中枢序列」，即线段中枢）；
signals 返回 dict 新增分组键 xd_beichi / xd_b23，同时段级信号并入合并
signals 列表（label 形如 段:S1 / 段:B1a / 段:B2 ...）。
"""
import csv
import unittest
from pathlib import Path

from chanapp.engine.signals import compute_signals
from chanapp.engine.structure import compute_structure

FIX_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    with open(FIX_DIR / name, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    bars = [
        {"dt": r["dt"], "open": float(r["open"]), "close": float(r["close"]),
         "high": float(r["high"]), "low": float(r["low"]),
         "volume": float(r["volume"])}
        for r in rows
    ]
    code = name.split("_")[0]
    structure = compute_structure(bars, code, "m30")
    sig = compute_signals(bars, structure)
    return {"structure": structure, "sig": sig}


class TestXdSignals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s1 = _load("sh000688_m30.csv")   # 科创50
        cls.s2 = _load("sz399006_m30.csv")   # 创业板指

    def test_structure_has_zs_xd(self):
        for s in (self.s1, self.s2):
            zs_xd = s["structure"]["zs_xd"]
            self.assertIsInstance(zs_xd, list)
            for z in zs_xd:
                for k in ("x0", "x1", "dt0", "dt1", "zg", "zd", "gg", "dd"):
                    self.assertIn(k, z)
                self.assertGreaterEqual(z["zg"], z["zd"])

    def test_kc50_xd_beichi_s1(self):
        hits = [(s["dt"], s["label"]) for s in self.s1["sig"]["xd_beichi"]]
        self.assertIn(("2026-07-01 10:30", "段:S1"), hits)   # 面积 877.02 vs 1128.04（已验证）
        hit = next(s for s in self.s1["sig"]["xd_beichi"]
                   if s["dt"] == "2026-07-01 10:30")
        self.assertAlmostEqual(hit["area_cur"], 877.02, places=2)
        self.assertAlmostEqual(hit["area_prev"], 1128.04, places=2)
        self.assertEqual(hit["side"], "sell")

    def test_cyb_xd_beichi_s1(self):
        hits = [(s["dt"], s["label"]) for s in self.s2["sig"]["xd_beichi"]]
        self.assertIn(("2026-06-25 15:00", "段:S1"), hits)   # 面积 916.89 vs 1999.88（已验证）
        hit = next(s for s in self.s2["sig"]["xd_beichi"]
                   if s["dt"] == "2026-06-25 15:00")
        self.assertAlmostEqual(hit["area_cur"], 916.89, places=2)
        self.assertAlmostEqual(hit["area_prev"], 1999.88, places=2)

    def test_xd_signals_prefixed_and_merged(self):
        sig = self.s1["sig"]
        for key in ("xd_beichi", "xd_b23"):
            for s in sig[key]:
                self.assertTrue(s["label"].startswith("段:"), s["label"])
        merged = [(s["dt"], s["label"]) for s in sig["signals"]
                  if s["label"].startswith("段:")]
        for s in sig["xd_beichi"] + sig["xd_b23"]:
            self.assertIn((s["dt"], s["label"]), merged)


if __name__ == "__main__":
    unittest.main()
