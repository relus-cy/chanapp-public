"""信号依据卡：为每个买卖点生成可读依据 dict。

输出 card：{type, dt, price, side, text, detail{...}}，按时间倒序由调用方排序。
文案示例（背驰）："锚 07-20@3741.11，面积 217.4 vs 243.1，DIF 24.2 vs 6.8，命中 area"。
段级信号（label 带 "段:" 前缀）剥前缀后走同一套文案，type 保留原 label。
"""
from __future__ import annotations

COND_TEXT = {"area": "面积", "dif": "DIF", "both": "面积+DIF"}


def _mmdd(dt: str) -> str:
    """展示层日期：'2026-08-03' → '08-03'；分钟级 '2026-08-03 14:30' → '08-03 14:30'。"""
    return dt[5:] if len(dt) >= 10 else dt


def build_evidence(signals: list[dict], structure: dict) -> list[dict]:
    """signals -> 依据卡列表（保持输入顺序，前端自行排序）。"""
    cards: list[dict] = []
    for s in signals:
        label = s["label"]
        seg = label.startswith("段:")
        base_label = label[2:] if seg else label   # 段级信号剥前缀走同一套文案
        base = {"type": label, "dt": s["dt"], "price": s["price"], "side": s["side"],
                "forming": s.get("forming", False)}
        if base_label.startswith(("B1a", "S1")):
            cond = s["cond"]
            text = (
                f"锚 {_mmdd(s['anchor_dt'])}@{s['anchor_price']:.2f}，"
                f"面积 {s['area_cur']} vs {s['area_prev']}，"
                f"DIF {s['dif_cur']} vs {s['dif_prev']}，"
                f"命中 {cond}"
            )
            if s.get("exhaustion_pct") is not None:
                text += f"，衰竭度 {s['exhaustion_pct']:.1f}%"
            if s.get("fallback"):
                unit = "段" if seg else "笔"
                text += f"（锚点为相邻同向{unit} fallback：不在任何线段内）"
            cards.append({**base, "text": text,
                          "detail": {"cond": cond, "cond_text": COND_TEXT[cond],
                                     "anchor_dt": s["anchor_dt"], "anchor_price": s["anchor_price"],
                                     "area_cur": s["area_cur"], "area_prev": s["area_prev"],
                                     "dif_cur": s["dif_cur"], "dif_prev": s["dif_prev"],
                                     "fallback": s.get("fallback", False)}})
        elif base_label in ("B3", "S3"):
            if base_label == "B3":
                text = (f"中枢[{_mmdd(s['zs_dt0'])}~{_mmdd(s['zs_dt1'])} "
                        f"上沿 {s['zg']:.2f}/下沿 {s['zd']:.2f}]，"
                        f"向上离开段 {_mmdd(s['leave_dt'])}@{s['leave_price']:.2f} 突破中枢上沿，"
                        f"回抽低点 {s['pull_price']:.2f} 未碰中枢上沿")
            else:
                text = (f"中枢[{_mmdd(s['zs_dt0'])}~{_mmdd(s['zs_dt1'])} "
                        f"上沿 {s['zg']:.2f}/下沿 {s['zd']:.2f}]，"
                        f"向下离开段 {_mmdd(s['leave_dt'])}@{s['leave_price']:.2f} 跌破中枢下沿，"
                        f"回抽高点 {s['pull_price']:.2f} 未碰中枢下沿")
            cards.append({**base, "text": text,
                          "detail": {"zg": s["zg"], "zd": s["zd"],
                                     "zs_dt0": s["zs_dt0"], "zs_dt1": s["zs_dt1"],
                                     "leave_dt": s["leave_dt"], "leave_price": s["leave_price"],
                                     "pull_price": s["pull_price"]}})
        elif base_label in ("B2", "S2"):
            if base_label == "B2":
                text = (f"B1a {_mmdd(s['anchor_dt'])}@{s['anchor_price']:.2f} 之后回抽笔 "
                        f"低点 {s['pull_price']:.2f} 未破前低（二买确认）")
            else:
                text = (f"S1 {_mmdd(s['anchor_dt'])}@{s['anchor_price']:.2f} 之后反弹笔 "
                        f"高点 {s['pull_price']:.2f} 未过前高（二卖确认）")
            cards.append({**base, "text": text,
                          "detail": {"anchor_dt": s["anchor_dt"], "anchor_price": s["anchor_price"],
                                     "pull_price": s["pull_price"]}})
    return cards
