"""唐奇安通道（engine/donchian.py）：入场线、离场线、N、规则推演与建议仓位。

先写下可能出错的方式，每条对应下面一个或几个用例：
- 入场线把当前 bar 也算进去：收盘永远不可能高于含自己的最高价，信号永远不出；
- 等于入场线也算突破（规则是严格大于）；
- N 用错算法或预热期错位（应为海龟的 Wilder 平滑：首值取前 n 根 TR 的均值，之后 (前值×(n−1)+TR)/n）；
- 不足 n 根时用残缺窗口算出数值（应为 null）；
- 入场那根就离场；有效离场价没取「离场线与止损线的较高者」；止损在持有期间被移动；
- 盘中触价跳空高开时按入场线而不是开盘价成交；
- 形成中的 bar 上的信号没标 provisional；收盘确认模式下盘中触到但收盘没站上的 bar 没标出来；
- 明日预估线没把最后一根算进去；
- 建议仓位没设上限或 N 为 0 时除零。
"""
import unittest

from chanapp.engine import donchian


def bars_from(rows, forming_last=False):
    """rows: [(open, high, low, close)]，日期顺延；forming_last 把最后一根标为形成中。"""
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        out.append({"dt": f"2026-01-{i + 1:02d}", "open": o, "high": h, "low": l, "close": c,
                    "volume": 1000, "forming": False})
    if forming_last and out:
        out[-1]["forming"] = True
    return out


P32 = (("3/2", 3, 2),)


def calc(rows, forming_last=False, atr_n=3):
    return donchian.compute(bars_from(rows, forming_last), params=P32, atr_n=atr_n)["3/2"]


# 前三根横盘（高 11 低 9），第 4 根收盘突破 11
FLAT = [(10, 11, 9, 10), (10, 11, 9, 10), (10, 11, 9, 10)]


class LinesTests(unittest.TestCase):
    def test_lines_exclude_current_bar_and_null_until_enough_bars(self):
        out = calc(FLAT + [(10, 12, 9.5, 11.5)])
        rows = out["rows"]
        self.assertEqual([r["entry"] for r in rows], [None, None, None, 11])
        self.assertEqual([r["exit"] for r in rows], [None, None, 9, 9])
        self.assertEqual([r["time"] for r in rows], ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"])

    def test_next_line_includes_last_bar(self):
        out = calc(FLAT + [(10, 12, 9.5, 11.5)])
        self.assertEqual(out["next"], {"entry": 12, "exit": 9})          # 最近 3 根最高 12，最近 2 根最低 9

    def test_next_line_null_until_enough_bars(self):
        out = calc(FLAT[:2])
        self.assertEqual(out["next"], {"entry": None, "exit": 9})

    def test_n_is_wilder_smoothed_true_range(self):
        # TR：2, 2, 2（前三根）；第 4 根 TR = max(12-9.5, |12-10|, |9.5-10|) = 2.5
        out = calc(FLAT + [(10, 12, 9.5, 11.5)])
        ns = [r["n"] for r in out["rows"]]
        self.assertEqual(ns[:2], [None, None])
        self.assertAlmostEqual(ns[2], 2.0)                               # 首值 = 前 3 根 TR 均值
        self.assertAlmostEqual(ns[3], (2.0 * 2 + 2.5) / 3)               # Wilder：(前值×2 + TR)/3


class CloseModeTests(unittest.TestCase):
    def test_close_above_entry_is_confirmed_buy(self):
        out = calc(FLAT + [(10, 12, 9.5, 11.5)])
        sig = out["modes"]["close"]["signals"]
        self.assertEqual(sig, [{"time": "2026-01-04", "side": "buy", "kind": "close", "status": "confirmed",
                                "price": 11.5, "level": 11}])
        st = out["modes"]["close"]["state"]
        n4 = (2.0 * 2 + 2.5) / 3
        self.assertTrue(st["holding"])
        self.assertEqual((st["entry_time"], st["entry_price"]), ("2026-01-04", 11.5))
        self.assertAlmostEqual(st["stop"], 11.5 - 2 * n4)

    def test_close_equal_to_entry_is_not_a_breakout(self):
        out = calc(FLAT + [(10, 11, 9.5, 11)])
        self.assertEqual(out["modes"]["close"]["signals"], [])
        self.assertEqual(out["modes"]["touch"]["signals"], [])          # 最高价等于入场线也不算触价
        self.assertFalse(out["modes"]["close"]["state"]["holding"])

    def test_touch_without_close_is_false_break_on_closed_bar(self):
        out = calc(FLAT + [(10, 12, 9.5, 10.5)])
        self.assertEqual(out["modes"]["close"]["signals"],
                         [{"time": "2026-01-04", "side": "buy", "kind": "false_break", "status": "confirmed",
                           "price": 11, "level": 11}])
        self.assertFalse(out["modes"]["close"]["state"]["holding"])

    def test_forming_bar_touch_is_provisional_touch_not_false_break(self):
        out = calc(FLAT + [(10, 12, 9.5, 10.5)], forming_last=True)
        self.assertEqual(out["modes"]["close"]["signals"],
                         [{"time": "2026-01-04", "side": "buy", "kind": "touch", "status": "provisional",
                           "price": 11, "level": 11}])

    def test_forming_bar_close_above_is_provisional_buy_and_state_says_so(self):
        out = calc(FLAT + [(10, 12, 9.5, 11.5)], forming_last=True)
        self.assertEqual(out["modes"]["close"]["signals"][0]["status"], "provisional")
        st = out["modes"]["close"]["state"]
        self.assertTrue(st["holding"])
        self.assertTrue(st["provisional"])

    def test_exit_uses_higher_of_exit_line_and_stop_and_never_on_entry_bar(self):
        # 第 4 根突破入场（收 11.5，N≈2.17，止损≈7.17）；离场线（前 2 根最低）在第 5 根是 9，第 6 根是 9.5
        rows = FLAT + [(10, 12, 9.5, 11.5), (11.5, 12, 10, 11), (11, 11, 9.4, 9.45)]
        out = calc(rows)
        sig = out["modes"]["close"]["signals"]
        self.assertEqual([(s["time"], s["side"], s["kind"]) for s in sig],
                         [("2026-01-04", "buy", "close"), ("2026-01-06", "sell", "close")])
        self.assertEqual((sig[1]["price"], sig[1]["level"]), (9.45, 9.5))   # 收盘价离场；有效离场价 = max(9.5, 止损≈7.17)
        self.assertFalse(out["modes"]["close"]["state"]["holding"])

    def test_stop_stays_fixed_while_holding(self):
        rows = FLAT + [(10, 12, 9.5, 11.5), (11.5, 20, 11, 19), (19, 30, 18, 29)]
        st = calc(rows)["modes"]["close"]["state"]
        n4 = (2.0 * 2 + 2.5) / 3
        self.assertTrue(st["holding"])
        self.assertAlmostEqual(st["stop"], 11.5 - 2 * n4)                  # 价格大涨，止损不动
        self.assertEqual(st["effective_exit"], 11)        # 下一根要用的离场线 = 最近 2 根最低 min(11, 18)，高于止损

    def test_stop_drives_exit_when_above_exit_line(self):
        # 低波动入场（N≈0.467，止损≈9.467）；第 5 根深探到 9.0 把离场线拉低，第 6 根由止损线决定离场
        rows = [(10, 10.2, 9.8, 10)] * 3 + [(10, 10.5, 9.9, 10.4), (10.4, 10.45, 9.0, 10.3), (10.3, 10.3, 9.2, 9.3)]
        out = calc(rows)
        sig = out["modes"]["close"]["signals"]
        stop = 10.4 - 2 * out["rows"][3]["n"]
        self.assertEqual([(s["time"], s["side"], s["kind"]) for s in sig],
                         [("2026-01-04", "buy", "close"),
                          ("2026-01-05", "sell", "false_break"),           # 深探 9.0 跌破离场线 9.8 但收回
                          ("2026-01-06", "sell", "close")])
        self.assertAlmostEqual(sig[2]["level"], stop)                       # max(离场线 9.0, 止损 9.467)
        self.assertEqual(sig[2]["price"], 9.3)

    def test_exit_touch_without_close_is_false_break(self):
        rows = FLAT + [(10, 12, 9.5, 11.5), (11.5, 12, 8.9, 11)]
        out = calc(rows)
        sig = out["modes"]["close"]["signals"]
        self.assertEqual(sig[-1], {"time": "2026-01-05", "side": "sell", "kind": "false_break",
                                   "status": "confirmed", "price": 9, "level": 9})
        self.assertTrue(out["modes"]["close"]["state"]["holding"])


class TouchModeTests(unittest.TestCase):
    def test_touch_enters_at_line_or_gap_open(self):
        out = calc(FLAT + [(10, 12, 9.5, 10.5)])
        sig = out["modes"]["touch"]["signals"]
        self.assertEqual(sig, [{"time": "2026-01-04", "side": "buy", "kind": "touch", "status": "confirmed",
                                "price": 11, "level": 11}])
        gap = calc(FLAT + [(11.8, 12, 11.6, 11.7)])
        self.assertEqual(gap["modes"]["touch"]["signals"][0]["price"], 11.8)
        st = gap["modes"]["touch"]["state"]
        self.assertAlmostEqual(st["stop"], 11.8 - 2 * 2.0)                 # 盘中下单时只知道上一根的 N

    def test_touch_on_forming_bar_is_provisional(self):
        out = calc(FLAT + [(10, 12, 9.5, 10.5)], forming_last=True)
        self.assertEqual(out["modes"]["touch"]["signals"][0]["status"], "provisional")

    def test_touch_mode_no_exit_on_entry_bar(self):
        # 第 4 根盘中触价入场后又跌穿离场线：当根不离场，下一根才离场
        rows = FLAT + [(10, 12, 8, 8.5), (8.5, 9, 7.9, 8.2)]
        sig = calc(rows)["modes"]["touch"]["signals"]
        self.assertEqual([(s["time"], s["side"]) for s in sig], [("2026-01-04", "buy"), ("2026-01-05", "sell")])
        self.assertEqual((sig[1]["price"], sig[1]["level"]), (8, 8))      # 离场线 = min(9, 8)，止损 7；按线价成交


class SuggestionTests(unittest.TestCase):
    def test_weight_is_risk_budget_over_stop_distance(self):
        out = calc(FLAT + [(10, 12, 9.5, 11.5)])
        n = out["rows"][-1]["n"]
        s = out["suggestion"]
        self.assertAlmostEqual(s["stop_distance"], 2 * n / 11.5)
        self.assertAlmostEqual(s["weight"], 0.01 / (2 * n / 11.5))
        self.assertEqual(s["risk_budget"], 0.01)

    def test_weight_capped_and_zero_n_safe(self):
        out = calc([(10, 10, 10, 10)] * 4)
        self.assertEqual(out["suggestion"]["weight"], 1.0)
        self.assertEqual(out["suggestion"]["stop_distance"], 0.0)

    def test_no_suggestion_without_n(self):
        self.assertIsNone(calc(FLAT[:2])["suggestion"])


class DefaultsTests(unittest.TestCase):
    def test_default_param_sets_and_turtle_n(self):
        rows = [(10 + i * 0.1, 10.5 + i * 0.1, 9.5 + i * 0.1, 10.2 + i * 0.1) for i in range(60)]
        out = donchian.compute(bars_from(rows))
        self.assertEqual(set(out), {"20/10", "55/20"})
        self.assertIsNone(out["20/10"]["rows"][19]["entry"])
        self.assertIsNotNone(out["20/10"]["rows"][20]["entry"])
        self.assertIsNone(out["55/20"]["rows"][54]["entry"])
        self.assertIsNone(out["20/10"]["rows"][18]["n"])                    # N 为 20 日：第 20 根起才有值
        self.assertIsNotNone(out["20/10"]["rows"][19]["n"])

    def test_empty_bars(self):
        out = donchian.compute([])
        self.assertEqual(out["20/10"]["rows"], [])
        self.assertEqual(out["20/10"]["next"], {"entry": None, "exit": None})
        self.assertIsNone(out["20/10"]["suggestion"])
        self.assertFalse(out["20/10"]["modes"]["close"]["state"]["holding"])


if __name__ == "__main__":
    unittest.main()
