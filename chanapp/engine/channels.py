"""通道线：最近一条已完成线段的上下轨（前端金色虚线）。

口径：
- 段选取：xds 中最后一条 forming=False 的已完成线段；没有已完成段时返回空列表
  （全部笔可能混着涨跌两腿，按任何单一方向画都会失真；与 chan.py 只在线段内
  算趋势线一致）；
- 下降段：上轨过段内每笔高端端点（max(y0,y1) 及其 x）中最高的一个，再连一个
  笔高点，取使段内其余笔高点都不在线上方的那一条（与 chan.py 外侧趋势线同口径）；
  只连最高两个高点时，后面的高点会越过上轨。下轨与上轨平行，过段内全部笔端点
  中相对该斜率最靠下的一个（y - 斜率·x 最小），不是绝对最低点：斜率陡时过绝对
  最低点的平行线会落到上轨上方。上轨锚点也是段内端点，所以下轨恒不高于上轨。
  上升段镜像（下轨过最低笔低点、段内笔低点都不在其下方，上轨过相对斜率最靠上
  的端点）；
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


def _outer_pair(points: list[tuple[float, int]], upper: bool) -> tuple | None:
    """边线的两个锚点（按 x 升序）：过最高（upper=True）/最低点，其余点都不越过这条线。

    先取最外点 A；A 之后有点时，取与 A 连线最平缓的那一点，后面的点都被压住（托住），
    A 之前的点自然在线内；A 是最后一点时，对 A 之前的点同理。并列时取离 A 最远的点。
    """
    s = 1 if upper else -1                       # 下边线按 -y 当上边线求
    pts = [(s * y, x) for y, x in points]
    ya, xa = max(pts, key=lambda p: (p[0], -p[1]))
    later = [p for p in pts if p[1] > xa]
    earlier = [p for p in pts if p[1] < xa]
    if later:
        yb, xb = max(later, key=lambda p: ((p[0] - ya) / (p[1] - xa), p[1]))
    elif earlier:
        yb, xb = min(earlier, key=lambda p: ((ya - p[0]) / (xa - p[1]), p[1]))
    else:
        return None
    return tuple(sorted([(s * ya, xa), (s * yb, xb)], key=lambda p: p[1]))


def _rail(p0: tuple[float, int], slope: float, x_end: int) -> dict:
    """过点 p0=(y,x)、斜率 slope 的线，延伸到 x_end。"""
    y0, x0 = p0
    return {"x0": x0, "y0": y0, "x1": x_end, "y1": y0 + slope * (x_end - x0)}


def build(bis: list[dict], xds: list[dict]) -> list[dict]:
    """由笔/线段序列构建通道列表（只产出最近已完成段的一条通道，没有已完成段时为空）。"""
    if not bis:
        return []
    completed = [x for x in xds if not x.get("forming")]
    if not completed:
        return []
    seg = completed[-1]
    seg_bis = [b for b in bis if b["x0"] >= seg["x0"] and b["x1"] <= seg["x1"]]
    direction = seg["direction"]
    if len(seg_bis) < 2:
        return []

    x_end = max(b["x1"] for b in bis)
    highs = [_hi(b) for b in seg_bis]
    lows = [_lo(b) for b in seg_bis]
    ends = highs + lows                            # 段内全部笔端点

    if direction == "down":
        pair = _outer_pair(highs, upper=True)      # 压住全部笔高点的边线 → 上轨
        if pair is None:
            return []
        (ya, xa), (yb, xb) = pair
        slope = (yb - ya) / (xb - xa)
        upper = _rail((ya, xa), slope, x_end)
        ye, xe = min(ends, key=lambda p: p[0] - slope * p[1])   # 相对斜率最低的端点 → 下轨（平行）
        lower = _rail((ye + slope * (xa - xe), xa), slope, x_end)
    else:
        pair = _outer_pair(lows, upper=False)      # 托住全部笔低点的边线 → 下轨
        if pair is None:
            return []
        (ya, xa), (yb, xb) = pair
        slope = (yb - ya) / (xb - xa)
        lower = _rail((ya, xa), slope, x_end)
        ye, xe = max(ends, key=lambda p: p[0] - slope * p[1])   # 相对斜率最高的端点 → 上轨（平行）
        upper = _rail((ye + slope * (xa - xe), xa), slope, x_end)

    return [{"upper": upper, "lower": lower, "direction": direction}]
