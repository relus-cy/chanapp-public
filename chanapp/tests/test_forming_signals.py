"""雏形买卖点（未确认笔预判）金标准（离线 fixture 驱动，不打外网）。

口径：对最新未完成笔，假设最新 bar 即笔终点，走 v2 背驰同一判定；
触发则 label 加 "~" 前缀（如 ~S1-d），否则 None。

金标准数值来自 fixture sh000688_m30.csv 截断后的离线计算（chanlun 2606.73，
MACD(12,26,9) hist=2*(DIF-DEA)），见 tmp/_t7_explore.py 演算记录。
"""
import csv
import unittest
from pathlib import Path

from chanapp.engine import signals
from chanapp.engine.structure import compute_structure

FIX = Path(__file__).parent / "fixtures" / "sh000688_m30.csv"


def _load_bars(cut_dt: str | None = None) -> list[dict]:
    with open(FIX, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    bars = [
        {"dt": r["dt"], "open": float(r["open"]), "close": float(r["close"]),
         "high": float(r["high"]), "low": float(r["low"]),
         "volume": float(r["volume"])}
        for r in rows
    ]
    if cut_dt is not None:
        cut = next(i for i, b in enumerate(bars) if b["dt"] == cut_dt)
        bars = bars[: cut + 1]
    return bars


class TestFormingSignal(unittest.TestCase):
    def test_forming_signal_on_extreme(self):
        # 截到 2026-08-18 10:00（科创50 顶，该 bar 为最新价）：末笔（向上）
        # 终点即最新 bar，创段内新高 1798.78，DIF 衰竭（13.161 < 14.036）
        # 而面积未缩（70.35 > 25.75）→ 仅 DIF 命中，应给出 ~S1-d
        bars = _load_bars("2026-08-18 10:00")
        st = compute_structure(bars, "sh000688", "m30")
        dif, _dea, hist = signals.macd([b["close"] for b in bars])

        sig = signals.detect_forming_signal(st["bi"], bars, hist, dif)

        self.assertIsNotNone(sig)
        self.assertEqual(sig["label"], "~S1-d")
        self.assertEqual(sig["dt"], "2026-08-18 10:00")
        self.assertAlmostEqual(sig["price"], 1798.78, places=2)
        self.assertEqual(sig["side"], "sell")
        self.assertEqual(sig["cond"], "dif")
        self.assertEqual(sig["anchor"],
                         {"dt": "2026-08-13 13:30", "price": 1782.78})
        self.assertEqual(sig["area_pair"], [70.35, 25.75])
        self.assertEqual(sig["dif_pair"], [13.161, 14.036])
        self.assertTrue(sig["forming"])

    def test_no_forming_signal_without_trigger(self):
        # 完整 fixture：最新未完成笔（向上）未创段内新高 → 不触发，返回 None
        bars = _load_bars()
        st = compute_structure(bars, "sh000688", "m30")
        dif, _dea, hist = signals.macd([b["close"] for b in bars])

        sig = signals.detect_forming_signal(st["bi"], bars, hist, dif, st["xd"])

        self.assertIsNone(sig)

    def test_compute_signals_exposes_forming_signal(self):
        bars = _load_bars("2026-08-18 10:00")
        st = compute_structure(bars, "sh000688", "m30")

        sig = signals.compute_signals(bars, st)

        self.assertIsNotNone(sig["forming_signal"])
        self.assertEqual(sig["forming_signal"]["label"], "~S1-d")


if __name__ == "__main__":
    unittest.main()
