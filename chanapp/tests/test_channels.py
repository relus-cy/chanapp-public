"""通道线（engine/channels.py）测试：合成 zigzag 精确几何 + 真实 fixture smoke。

口径：
- 取最近一条已完成线段（forming=False；xds 为空时退化为全部笔视作一段）；
- 下降段：段内最高两个笔高点（每笔取高端端点 max(y0,y1)）连成上轨，
  下轨为过段内最低点（笔低端端点最小值）的平行线；上升段镜像；
- x1 延伸到最后已知 bar（段内笔的最大 x1），y1 为该处的延长值。
"""
import csv
import unittest
from pathlib import Path

from chanapp.engine import channels
from chanapp.engine.structure import compute_structure

FIX = Path(__file__).parent / "fixtures" / "sh000688_m30.csv"

# 下降段：高点 100→90→80（共线，斜率 -1/bar），低点 80→70→60
DOWN_BIS = [
    {"x0": 0, "x1": 5, "y0": 100.0, "y1": 80.0, "direction": "down"},
    {"x0": 5, "x1": 10, "y0": 80.0, "y1": 90.0, "direction": "up"},
    {"x0": 10, "x1": 15, "y0": 90.0, "y1": 70.0, "direction": "down"},
    {"x0": 15, "x1": 20, "y0": 70.0, "y1": 80.0, "direction": "up"},
    {"x0": 20, "x1": 25, "y0": 80.0, "y1": 60.0, "direction": "down"},
]

# 上升段（镜像）：低点 20→30→40（共线，斜率 +1/bar），高点 40→50→60
UP_BIS = [
    {"x0": 0, "x1": 5, "y0": 20.0, "y1": 40.0, "direction": "up"},
    {"x0": 5, "x1": 10, "y0": 40.0, "y1": 30.0, "direction": "down"},
    {"x0": 10, "x1": 15, "y0": 30.0, "y1": 50.0, "direction": "up"},
    {"x0": 15, "x1": 20, "y0": 50.0, "y1": 40.0, "direction": "down"},
    {"x0": 20, "x1": 25, "y0": 40.0, "y1": 60.0, "direction": "up"},
]


class TestChannelGeometry(unittest.TestCase):
    def test_down_channel_geometry(self):
        ch = channels.build(DOWN_BIS, [])[-1]
        self.assertEqual(ch["direction"], "down")
        # 上轨过 (0,100)-(20,80)：斜率 -1/bar → x=25 时 y=75
        self.assertAlmostEqual(ch["upper"]["y1"], 75.0, places=6)
        # 下轨平行过最低点 (25,60)
        self.assertAlmostEqual(ch["lower"]["y1"], 60.0, places=6)
        # 上轨锚在最高两笔高点 (0,100)/(10,90)（与 (20,80) 共线）
        self.assertEqual(ch["upper"]["x0"], 0)
        self.assertAlmostEqual(ch["upper"]["y0"], 100.0, places=6)
        # 两轨平行且上轨恒在下轨上方
        slope_u = (ch["upper"]["y1"] - ch["upper"]["y0"]) / \
            (ch["upper"]["x1"] - ch["upper"]["x0"])
        slope_l = (ch["lower"]["y1"] - ch["lower"]["y0"]) / \
            (ch["lower"]["x1"] - ch["lower"]["x0"])
        self.assertAlmostEqual(slope_u, slope_l, places=9)
        self.assertGreater(ch["upper"]["y1"], ch["lower"]["y1"])

    def test_up_channel_geometry(self):
        ch = channels.build(UP_BIS, [])[-1]
        self.assertEqual(ch["direction"], "up")
        # 下轨过 (0,20)-(20,40)：斜率 +1/bar → x=25 时 y=45
        self.assertAlmostEqual(ch["lower"]["y1"], 45.0, places=6)
        # 上轨平行过最高点 (25,60)
        self.assertAlmostEqual(ch["upper"]["y1"], 60.0, places=6)

    def test_uses_last_completed_xd(self):
        # xds 指定最近已完成段（下降，x 10–25），更早的上升段不产出通道
        xds = [
            {"x0": 0, "x1": 10, "y0": 100.0, "y1": 90.0, "direction": "up",
             "forming": False},
            {"x0": 10, "x1": 25, "y0": 90.0, "y1": 60.0, "direction": "down",
             "forming": False},
        ]
        chs = channels.build(DOWN_BIS, xds)
        self.assertEqual(len(chs), 1)
        self.assertEqual(chs[0]["direction"], "down")
        # 段内笔高端点：90@10、90@10（笔3 起点）、80@20 → 上轨过 (10,90)/(20,80)
        # 斜率 -1/bar → x=25 时 y=75
        self.assertAlmostEqual(chs[0]["upper"]["y1"], 75.0, places=6)
        self.assertAlmostEqual(chs[0]["lower"]["y1"], 60.0, places=6)

    def test_skips_forming_xd(self):
        xds = [
            {"x0": 0, "x1": 25, "y0": 100.0, "y1": 60.0, "direction": "down",
             "forming": False},
            {"x0": 25, "x1": 30, "y0": 60.0, "y1": 70.0, "direction": "up",
             "forming": True},
        ]
        chs = channels.build(DOWN_BIS, xds)
        self.assertEqual(chs[-1]["direction"], "down")

    def test_too_few_bis_returns_empty(self):
        self.assertEqual(channels.build([], []), [])
        self.assertEqual(channels.build([DOWN_BIS[0]], []), [])


class TestChannelFixtureSmoke(unittest.TestCase):
    """真实 fixture（科创50 m30）smoke：通道数量、方向与字段完整性。"""

    @classmethod
    def setUpClass(cls):
        with open(FIX, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        cls.bars = [
            {"dt": r["dt"], "open": float(r["open"]), "close": float(r["close"]),
             "high": float(r["high"]), "low": float(r["low"]),
             "volume": float(r["volume"])}
            for r in rows
        ]
        cls.structure = compute_structure(cls.bars, "sh000688", "m30")
        cls.channels = channels.build(cls.structure["bi"], cls.structure["xd"])

    def test_has_channel_matching_last_completed_xd(self):
        xds = [x for x in self.structure["xd"] if not x.get("forming")]
        self.assertGreaterEqual(len(self.channels), 1)
        ch = self.channels[-1]
        self.assertEqual(ch["direction"], xds[-1]["direction"])

    def test_rail_fields_and_bounds(self):
        n = len(self.bars)
        for ch in self.channels:
            for rail in ("upper", "lower"):
                r = ch[rail]
                for k in ("x0", "y0", "x1", "y1"):
                    self.assertIn(k, r)
                self.assertGreaterEqual(r["x0"], 0)
                self.assertLessEqual(r["x1"], n - 1)
                self.assertLess(r["x0"], r["x1"])
            # 两轨平行
            su = (ch["upper"]["y1"] - ch["upper"]["y0"]) / \
                (ch["upper"]["x1"] - ch["upper"]["x0"])
            sl = (ch["lower"]["y1"] - ch["lower"]["y0"]) / \
                (ch["lower"]["x1"] - ch["lower"]["x0"])
            self.assertAlmostEqual(su, sl, places=9)
            # 上轨在段内不跌破下轨（同 x 处 upper >= lower）
            self.assertGreaterEqual(ch["upper"]["y1"], ch["lower"]["y1"])


if __name__ == "__main__":
    unittest.main()
