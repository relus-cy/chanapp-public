"""背驰衰竭度（area_ratio / exhaustion_pct）与 MACD 背驰连线金标准（离线 fixture）。

口径：背驰信号元素增加 area_ratio（当前面积/锚点面积）与
exhaustion_pct = (1 - area_ratio) * 100，仅面积命中（cond in {area, both}）时
有值，DIF 命中（cond == "dif"）两者均为 None；evidence 文案追加「衰竭度 xx.x%」。

金标准：科创50 m30（fixture sh000688_m30.csv）
- B1a @ 2026-08-03 14:30：面积 67.32 vs 267.75 → ratio ≈ 0.2514，衰竭度 ≈ 74.9%
- S1-d @ 2026-08-18 10:00：仅 DIF 命中 → exhaustion_pct is None
"""
import csv
import unittest
from pathlib import Path

from chanapp.engine import evidence as engine_evidence
from chanapp.engine.signals import compute_signals
from chanapp.engine.structure import compute_structure

FIX = Path(__file__).parent / "fixtures" / "sh000688_m30.csv"

BEICHI_LABELS = {"B1a", "S1", "B1a-d", "S1-d"}


def _load() -> dict:
    with open(FIX, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    bars = [
        {"dt": r["dt"], "open": float(r["open"]), "close": float(r["close"]),
         "high": float(r["high"]), "low": float(r["low"]),
         "volume": float(r["volume"])}
        for r in rows
    ]
    structure = compute_structure(bars, "sh000688", "m30")
    sig = compute_signals(bars, structure)
    return {"bars": bars, "structure": structure, "sig": sig}


class TestExhaustion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = _load()
        cls.beichi = [s for s in cls.data["sig"]["signals"]
                      if s["label"] in BEICHI_LABELS]

    def beichi_of(self, dt: str) -> dict:
        return next(s for s in self.beichi if s["dt"] == dt)

    def test_exhaustion_pct_kc50(self):
        s = self.beichi_of("2026-08-03 14:30")   # B1a，面积 67.32 vs 267.75 [both]
        self.assertAlmostEqual(s["area_ratio"], 67.32 / 267.75, places=3)
        self.assertAlmostEqual(s["exhaustion_pct"], 74.9, delta=0.2)

    def test_dif_only_hit_has_none_ratio(self):
        s = self.beichi_of("2026-08-18 10:00")   # S1-d，仅 DIF 命中
        self.assertIsNone(s["exhaustion_pct"])
        self.assertIsNone(s["area_ratio"])

    def test_evidence_text_appends_exhaustion(self):
        cards = engine_evidence.build_evidence(self.data["sig"]["signals"],
                                               self.data["structure"])
        card = next(c for c in cards if c["dt"] == "2026-08-03 14:30"
                    and c["type"] == "B1a")
        self.assertIn("衰竭度 74.9%", card["text"])
        dif_card = next(c for c in cards if c["dt"] == "2026-08-18 10:00"
                        and c["type"] == "S1-d")
        self.assertNotIn("衰竭度", dif_card["text"])

    def test_beichi_link_fields(self):
        # beichi_links 依赖的锚点 bar 下标：信号元素需暴露 anchor_x
        s = self.beichi_of("2026-08-03 14:30")
        bars = self.data["bars"]
        self.assertEqual(bars[s["anchor_x"]]["dt"], s["anchor_dt"])
        self.assertEqual(bars[s["x"]]["dt"], s["dt"])


if __name__ == "__main__":
    unittest.main()
