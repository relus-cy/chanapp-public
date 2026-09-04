"""chanapp engine 金标准回归测试（sh000001 日线，qfq 冻结 fixture）。

金标准事实来自仓库 HANDOFF.md「已确认的结构结论（sh000001 日线）」：
- 2026-07-20 @ 3741.11：向下笔（06-23→07-20）与向下线段（05-14→07-20）终点；
- 2026-08-18 @ 3994.18：向上笔终点；
- 中枢 [2025-10-30 ~ 2026-08-18, ZG 4025.70 / ZD 3922.58]（±1.0）。

数据固定用 tests/fixtures/sh000001_day_qfq.csv（2026-08-21 抓取的 800 根快照），
保证测试可重复、不打外网。

运行：.venv-chan/bin/python -m unittest discover -s chanapp/tests -v
"""
import csv
import unittest
from datetime import datetime
from pathlib import Path

from chanapp.engine import evidence, signals, structure

FIXTURE = Path(__file__).parent / "fixtures" / "sh000001_day_qfq.csv"

PRICE_TOL = 0.01   # 端点价格须精确一致
ZS_TOL = 1.0       # 中枢 ZG/ZD 容差


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        return [
            {
                "dt": r["dt"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
            }
            for r in csv.DictReader(f)
        ]


class TestGoldenSh000001Day(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = load_fixture()
        cls.structure = structure.compute_structure(cls.bars, "sh000001", "day")
        cls.sig = signals.compute_signals(cls.bars, cls.structure)
        cls.evidence = evidence.build_evidence(cls.sig["signals"], cls.structure)

    def test_bi_endpoint_0720_down(self):
        """2026-07-20 @ 3741.11 为向下笔终点。"""
        hits = [b for b in self.structure["bi"]
                if b["dt1"] == "2026-07-20" and abs(b["y1"] - 3741.11) <= PRICE_TOL]
        self.assertTrue(hits, "缺少 2026-07-20@3741.11 的笔端点")
        self.assertEqual(hits[0]["direction"], "down")

    def test_bi_endpoint_0818_up(self):
        """2026-08-18 @ 3994.18 为向上笔终点。"""
        hits = [b for b in self.structure["bi"]
                if b["dt1"] == "2026-08-18" and abs(b["y1"] - 3994.18) <= PRICE_TOL]
        self.assertTrue(hits, "缺少 2026-08-18@3994.18 的笔端点")
        self.assertEqual(hits[0]["direction"], "up")

    def test_xd_down_0514_0720(self):
        """线段含 05-14 → 07-20 向下，终点 3741.11。"""
        hits = [x for x in self.structure["xd"]
                if x["dt1"] == "2026-07-20" and abs(x["y1"] - 3741.11) <= PRICE_TOL
                and x["direction"] == "down"]
        self.assertTrue(hits, "缺少 05-14→07-20 向下线段")
        self.assertTrue(hits[0]["dt0"] <= "2026-05-14")

    def test_xd_up_0720_0818(self):
        """线段含 07-20 → 08-18 向上，终点 3994.18。"""
        hits = [x for x in self.structure["xd"]
                if x["dt0"] == "2026-07-20" and x["dt1"] == "2026-08-18"
                and abs(x["y1"] - 3994.18) <= PRICE_TOL and x["direction"] == "up"]
        self.assertTrue(hits, "缺少 07-20→08-18 向上线段")

    def test_zs_golden_range(self):
        """中枢含 ZG≈4025.70 / ZD≈3922.58（±1.0）。"""
        hits = [z for z in self.structure["zs"]
                if abs(z["zg"] - 4025.70) <= ZS_TOL and abs(z["zd"] - 3922.58) <= ZS_TOL]
        self.assertTrue(hits, "缺少 ZG≈4025.70/ZD≈3922.58 的中枢；实际："
                        + str([(z["dt0"], z["dt1"], round(z["zg"], 2), round(z["zd"], 2))
                               for z in self.structure["zs"][-5:]]))

    def test_pipeline_produces_signals_and_evidence(self):
        """信号/依据卡管线不空且口径字段齐全（背驰卡含锚点/面积对/DIF对/命中条件）。"""
        self.assertTrue(self.structure["counts"]["bi"] > 0)
        self.assertTrue(self.sig["signals"], "无信号")
        self.assertEqual(len(self.evidence), len(self.sig["signals"]))
        beichi_cards = [c for c in self.evidence if c["type"].startswith(("B1a", "S1"))]
        for c in beichi_cards:
            d = c["detail"]
            for k in ("anchor_dt", "anchor_price", "area_cur", "area_prev",
                      "dif_cur", "dif_prev", "cond"):
                self.assertIn(k, d)
            self.assertIn("命中", c["text"])

    def test_evidence_text_plain_language(self):
        """B3/S3 依据卡用白话术语（中枢上沿/下沿），不出 ZG/ZD 缩写。"""
        zs3_cards = [c for c in self.evidence
                     if c["type"].startswith(("B3", "S3", "段:B3", "段:S3"))]
        self.assertTrue(zs3_cards, "fixture 应产出 B3/S3 依据卡")
        for c in zs3_cards:
            edge = "中枢上沿" if c["type"].removeprefix("段:").startswith("B3") else "中枢下沿"
            self.assertIn(edge, c["text"])
            self.assertNotIn("ZG", c["text"])
            self.assertNotIn("ZD", c["text"])

    def test_macd_lengths(self):
        m = self.sig["macd"]
        n = len(self.bars)
        self.assertEqual(len(m["dif"]), n)
        self.assertEqual(len(m["dea"]), n)
        self.assertEqual(len(m["hist"]), n)


class TestMinuteFreqStructure(unittest.TestCase):
    """v1.4.1 放开 m15/m5：结构计算接受新周期（FREQ_SECONDS 映射齐备）。"""

    def _minute_bars(self, step_min):
        """合成 200 根锯齿分钟 bar（足够出笔/线段）。"""
        bars = []
        base = datetime(2026, 9, 1, 9, 30).timestamp()
        price = 100.0
        for i in range(200):
            price += 1.0 if (i // 5) % 2 == 0 else -1.0
            dt = datetime.fromtimestamp(base + i * step_min * 60).strftime("%Y-%m-%d %H:%M")
            bars.append({"dt": dt, "open": price - 0.2, "high": price + 0.5,
                         "low": price - 0.6, "close": price, "volume": 1000.0})
        return bars

    def test_m15_m5_structure(self):
        for freq, step in (("m15", 15), ("m5", 5)):
            s = structure.compute_structure(self._minute_bars(step), "sh000001", freq)
            self.assertIn("bi", s)
            self.assertIn("zs", s)
            self.assertGreater(len(s["bi"]), 0, f"{freq} 应能出笔")


if __name__ == "__main__":
    unittest.main()
