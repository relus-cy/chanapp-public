"""通道线：最近一条已完成线段的上下轨（前端金色虚线）。

口径：
- 段选取：xds 中最后一条 forming=False 的已完成线段；xds 为空（或无已完成段）
  时退化为把全部笔视作一段，方向取末笔方向；
- 下降段：段内每笔的高端端点（max(y0,y1) 及其 x）中最高的两个连成上轨，
  下轨为过段内最低点（低端端点最小值）的平行线；上升段镜像（最低两笔低点
  连成下轨，上轨过段内最高点）；
- x1 延伸到最后已知 bar（全部笔的最大 x1），y1 为该处延长值。

元素：{"upper": {x0,y0,x1,y1}, "lower": {x0,y0,x1,y1}, "direction": "up"|"down"}
"""
from __future__ import annotations


def _hi(b: dict) -> tuple[float, int]:
    """笔的高端端点 (y, x)。"""
    return (b["y0"], b["x0"]) if b["y0"] >= b["y1"] else (b["y1"], b["x1"])


def _lo(b: dict) -> tuple[float, int]:
    """笔的低端端点 (y, x)。"""
    return (b["y0"], b["x0"]) if b["y0"] <= b["y1"] else (b["y1"], b["x1"])


def _top_two(points: list[tuple[float, int]], reverse: bool) -> tuple | None:
    """取 y 最大（reverse=True）/最小的两个 x 不同的点，按 x 升序返回。"""
    ordered = sorted(points, key=lambda p: (p[0], -p[1]), reverse=reverse)
    first = ordered[0]
    second = next((p for p in ordered[1:] if p[1] != first[1]), None)
    if second is None:
        return None
    return tuple(sorted([first, second], key=lambda p: p[1]))


def _rail(p0: tuple[float, int], slope: float, x_end: int) -> dict:
    """过点 p0=(y,x)、斜率 slope 的线，延伸到 x_end。"""
    y0, x0 = p0
    return {"x0": x0, "y0": y0, "x1": x_end, "y1": y0 + slope * (x_end - x0)}


def build(bis: list[dict], xds: list[dict]) -> list[dict]:
    """由笔/线段序列构建通道列表（当前只产出最近一段的一条通道）。"""
    if not bis:
        return []
    completed = [x for x in xds if not x.get("forming")]
    if completed:
        seg = completed[-1]
        seg_bis = [b for b in bis if b["x0"] >= seg["x0"] and b["x1"] <= seg["x1"]]
        direction = seg["direction"]
    else:
        seg_bis = list(bis)
        direction = bis[-1]["direction"]
    if len(seg_bis) < 2:
        return []

    x_end = max(b["x1"] for b in bis)
    highs = [_hi(b) for b in seg_bis]
    lows = [_lo(b) for b in seg_bis]

    if direction == "down":
        pair = _top_two(highs, reverse=True)      # 最高两个笔高点 → 上轨
        if pair is None:
            return []
        (ya, xa), (yb, xb) = pair
        slope = (yb - ya) / (xb - xa)
        upper = _rail((ya, xa), slope, x_end)
        ye, xe = min(lows)                         # 段内最低点 → 下轨（平行）
        lower = _rail((ye + slope * (xa - xe), xa), slope, x_end)
    else:
        pair = _top_two(lows, reverse=False)      # 最低两个笔低点 → 下轨
        if pair is None:
            return []
        (ya, xa), (yb, xb) = pair
        slope = (yb - ya) / (xb - xa)
        lower = _rail((ya, xa), slope, x_end)
        ye, xe = max(highs)                        # 段内最高点 → 上轨（平行）
        upper = _rail((ye + slope * (xa - xe), xa), slope, x_end)

    return [{"upper": upper, "lower": lower, "direction": direction}]
