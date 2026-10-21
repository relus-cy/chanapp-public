"""通道线（engine/channels.py）测试：合成 zigzag 精确几何 + 真实 fixture smoke。

口径：
- 取最近一条已完成线段（forming=False）；没有已完成段时不画通道；
- 下降段：段内最高两个笔高点（每笔取高端端点 max(y0,y1)）连成上轨，
  下轨为平行线，过段内相对上轨斜率最靠下的笔端点；上升段镜像；
- x1 延伸到最后已知 bar（段内笔的最大 x1），y1 为该处的延长值。

可能出错的方式：没有已完成段时把涨跌两腿混成一段、按末笔方向画；两点连线斜率陡或
与段方向相反时，过绝对最低（高）点的平行线落到另一轨的对侧，上轨低于下轨。
"""
import csv
import unittest
from pathlib import Path

from chanapp.engine import channels
from chanapp.engine.chanpy_adapter import compute_analysis

FIX = Path(__file__).parent / "fixtures" / "sh000688_m30.csv"
FIX_SH_DAY = Path(__file__).parent / "fixtures" / "sh000001_day_qfq.csv"

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


# demo 样本 sz300308 日线的笔（/api/chart 实测）：先涨到 1416.88 再跌到 793.03，
# 两条线段都还在形成中。旧退路按末笔方向画下降通道，上轨 658.02 低于下轨 676.09
SZ300308_DAY_BIS = [
    {"x0": 9, "x1": 19, "y0": 336.6017, "y1": 541.3693, "direction": "up"},
    {"x0": 19, "x1": 29, "y0": 541.3693, "y1": 449.4781, "direction": "down"},
    {"x0": 29, "x1": 60, "y0": 449.4781, "y1": 658.0213, "direction": "up"},
    {"x0": 60, "x1": 70, "y0": 658.0213, "y1": 558.1894, "direction": "down"},
    {"x0": 70, "x1": 74, "y0": 558.1894, "y1": 644.2376, "direction": "up"},
    {"x0": 74, "x1": 94, "y0": 644.2376, "y1": 514.3913, "direction": "down"},
    {"x0": 94, "x1": 100, "y0": 514.3913, "y1": 598.3319, "direction": "up"},
    {"x0": 100, "x1": 104, "y0": 598.3319, "y1": 505.4019, "direction": "down"},
    {"x0": 104, "x1": 174, "y0": 505.4019, "y1": 1416.88, "direction": "up"},
    {"x0": 174, "x1": 202, "y0": 1416.88, "y1": 793.03, "direction": "down"},
]
SZ300308_DAY_XDS = [
    {"x0": 9, "x1": 60, "y0": 336.6017, "y1": 658.0213, "direction": "up", "forming": True},
    {"x0": 60, "x1": 104, "y0": 658.0213, "y1": 505.4019, "direction": "down", "forming": True},
]

# 下降段：最高两个高点 100@0、60@10 连线斜率 -4/bar，最低点 40@30 在这条线上方；
# 过最低点的平行线在 x=0 处为 160，高于上轨的 100
STEEP_DOWN_BIS = [
    {"x0": 0, "x1": 5, "y0": 100.0, "y1": 50.0, "direction": "down"},
    {"x0": 5, "x1": 10, "y0": 50.0, "y1": 60.0, "direction": "up"},
    {"x0": 10, "x1": 30, "y0": 60.0, "y1": 40.0, "direction": "down"},
]


def whole(bis: list[dict], direction: str) -> dict:
    """覆盖全部笔的一条已完成线段。"""
    return {"x0": bis[0]["x0"], "x1": bis[-1]["x1"], "y0": bis[0]["y0"], "y1": bis[-1]["y1"],
            "direction": direction, "forming": False}


def at(rail: dict, x: float) -> float:
    """轨道在 x 处的值。"""
    return rail["y0"] + (rail["y1"] - rail["y0"]) * (x - rail["x0"]) / (rail["x1"] - rail["x0"])


class RailOrderMixin:
    def assertRailsOrdered(self, ch: dict, msg=None):
        """两轨起止 x 相同（即对外的 t0、t1），且在两处都是上轨高于下轨。"""
        u, lo = ch["upper"], ch["lower"]
        self.assertEqual((u["x0"], u["x1"]), (lo["x0"], lo["x1"]), msg)
        self.assertGreater(u["y0"], lo["y0"], msg)
        self.assertGreater(u["y1"], lo["y1"], msg)


class TestChannelGeometry(RailOrderMixin, unittest.TestCase):
    def test_down_channel_geometry(self):
        ch = channels.build(DOWN_BIS, [whole(DOWN_BIS, "down")])[-1]
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
        ch = channels.build(UP_BIS, [whole(UP_BIS, "up")])[-1]
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
        self.assertEqual(channels.build([DOWN_BIS[0]], [whole([DOWN_BIS[0]], "down")]), [])

    def test_no_completed_segment_returns_empty(self):
        # 没有已完成段就没有可画的段：全部笔混着涨跌两腿，哪个方向都会画错
        self.assertEqual(channels.build(SZ300308_DAY_BIS, SZ300308_DAY_XDS), [])
        self.assertEqual(channels.build(SZ300308_DAY_BIS, []), [])

    def test_steep_pair_keeps_upper_above_lower(self):
        ch = channels.build(STEEP_DOWN_BIS, [whole(STEEP_DOWN_BIS, "down")])[-1]
        self.assertEqual(ch["direction"], "down")
        self.assertRailsOrdered(ch)
        # 上轨仍过最高两个高点 (0,100)-(10,60)；下轨过相对斜率最靠下的端点 (5,50)
        self.assertEqual((ch["upper"]["x0"], ch["upper"]["x1"]), (0, 30))
        self.assertAlmostEqual(ch["upper"]["y0"], 100.0, places=6)
        self.assertAlmostEqual(ch["upper"]["y1"], -20.0, places=6)
        self.assertAlmostEqual(ch["lower"]["y0"], 70.0, places=6)
        self.assertAlmostEqual(ch["lower"]["y1"], -50.0, places=6)
        # 段内每个笔端点都不在下轨下方
        for b in STEEP_DOWN_BIS:
            for x, y in ((b["x0"], b["y0"]), (b["x1"], b["y1"])):
                self.assertGreaterEqual(y, at(ch["lower"], x) - 1e-9)


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
        cls.structure = compute_analysis(cls.bars, "m30")["structure"]
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


class TestChannelOrderOnFixture(RailOrderMixin, unittest.TestCase):
    """上证日线 fixture：逐条已完成线段单独建通道，每条都是上轨高于下轨。

    旧口径下 1–186 的下降段与 186–250 的上升段两轨倒挂。"""

    @classmethod
    def setUpClass(cls):
        with open(FIX_SH_DAY, encoding="utf-8") as f:
            bars = [{"dt": r["dt"], **{k: float(r[k]) for k in ("open", "high", "low", "close", "volume")}}
                    for r in csv.DictReader(f)]
        cls.structure = compute_analysis(bars, "day")["structure"]

    def test_every_completed_segment_channel_is_ordered(self):
        bis, xds = self.structure["bi"], self.structure["xd"]
        done = [i for i, x in enumerate(xds) if not x.get("forming")]
        self.assertGreaterEqual(len(done), 6)
        for i in done:
            chs = channels.build(bis, xds[:i + 1])
            self.assertEqual(len(chs), 1, f"xd {xds[i]['x0']}-{xds[i]['x1']}")
            self.assertRailsOrdered(chs[0], f"xd {xds[i]['x0']}-{xds[i]['x1']}")


if __name__ == "__main__":
    unittest.main()
