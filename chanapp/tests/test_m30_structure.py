"""m30 结构金标准回归（离线 fixture 驱动，不打外网）。

金标准数值来自 tmp/chan-validation/run_chanlun_m30_beichi.py 已验证输出
（chanlun 2606.73，观察者周期 1800s，MACD(12,26,9) hist=2*(DIF-DEA)）。

注意：compute_signals 实际返回 {"macd", "signals", "forming"}，无独立 "beichi" 键；
背驰命中在合并的 signals 列表中（label 为 B1a/S1/S1-d 等），此处按计划原意
从合并列表过滤出背驰命中做断言。
"""
import csv
import unittest
from pathlib import Path

from chanapp.engine.signals import compute_signals
from chanapp.engine.structure import compute_structure

FIX_DIR = Path(__file__).parent / "fixtures"

BEICHI_LABELS = {"B1a", "S1", "B1a-d", "S1-d"}


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
    beichi = [s for s in sig["signals"] if s["label"] in BEICHI_LABELS]
    return {"structure": structure, "signals": {"beichi": beichi, "all": sig["signals"]}}


class TestM30Structure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s1 = _load("sh000688_m30.csv")   # 科创50，800 根 mkline 格式
        cls.s2 = _load("sz399006_m30.csv")   # 创业板指

    def test_kc50_bi_endpoints(self):
        ep = {(b["dt0"], b["dt1"]) for b in self.s1["structure"]["bi"]}
        self.assertIn(("2026-07-24 13:30", "2026-07-30 13:30"), ep)
        self.assertIn(("2026-08-14 13:30", "2026-08-18 10:00"), ep)

    def test_kc50_beichi_b1a_0803(self):
        hits = [(s["dt"], s["label"]) for s in self.s1["signals"]["beichi"]]
        self.assertIn(("2026-08-03 14:30", "B1a"), hits)   # 面积 67.32 vs 267.75 [both]
        hit = next(s for s in self.s1["signals"]["beichi"]
                   if s["dt"] == "2026-08-03 14:30")
        self.assertAlmostEqual(hit["area_cur"], 67.32, places=2)
        self.assertAlmostEqual(hit["area_prev"], 267.75, places=2)
        self.assertEqual(hit["cond"], "both")

    def test_cyb_beichi_b1a_0730(self):
        hits = [(s["dt"], s["label"]) for s in self.s2["signals"]["beichi"]]
        self.assertIn(("2026-07-30 13:30", "B1a"), hits)   # 面积 369.51 vs 602.93 [both]
        hit = next(s for s in self.s2["signals"]["beichi"]
                   if s["dt"] == "2026-07-30 13:30")
        self.assertAlmostEqual(hit["area_cur"], 369.51, places=2)
        self.assertAlmostEqual(hit["area_prev"], 602.93, places=2)
        self.assertEqual(hit["cond"], "both")

    def test_xd_endpoint_0818(self):
        # chanapp 在确认线段后追加 forming 线段，金标准对应最后一条「已确认」线段
        xds = [x for x in self.s1["structure"]["xd"] if not x.get("forming")]
        self.assertEqual(xds[-1]["dt1"], "2026-08-18 10:00")
        self.assertAlmostEqual(xds[-1]["y1"], 1798.78, places=2)


if __name__ == "__main__":
    unittest.main()
