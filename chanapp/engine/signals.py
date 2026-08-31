"""买卖点信号：v2 背驰（B1a/S1）+ 三类买卖点 + 雏形笔。

规则口径：
- MACD(12,26,9)，EMA alpha=2/(n+1) 首值种子；hist = 2*(DIF-DEA)（A股软件通行口径）。
- 背驰 v2：段内纪录极值锚点（所属 chanlun 线段内，同向笔取价格最极端者）；
  不属于任何线段的笔 fallback 到相邻同向笔（fallback=true）。
  触发 = 价格新极值（向下笔终点创新低 / 向上笔终点创新高，相对锚点笔终点价）
        且（笔面积缩小 或 终点 DIF 衰竭）二选一；cond=area/dif/both，
        仅 DIF 命中标签加 -d 后缀。向下笔触发为买点 B1a，向上笔为卖点 S1。
- 笔面积：区间 (start_bar, end_bar] 内，向下笔累加 hist<0 的绝对值，向上笔累加 hist>0。
- 三买 B3：中枢之后向上离开段突破 ZG，随后向下回抽笔低点不碰 ZG；三卖 S3 镜像。
- 二买 B2：B1a 之后第一根向下回抽笔终点不破 B1a 低点（前低）；二卖 S2 镜像（不破前高）。
- 雏形（forming）：最新未完成笔 = 最后确认笔终点之后，沿反方向走到区间内极值，标 forming。
- 雏形买卖点（forming_signal）：对最新未完成笔假设最新 bar 即笔终点，走 v2 同一
  判定做预判；触发则输出 label 带 "~" 前缀的信号（如 ~S1-d），否则 None。
- 段级信号：xd 当 bi、zs_xd（线段中枢）当 zs 复用同一组检测；xd 无上级线段，
  背驰锚点为相邻同向线段（v2 fallback 路径）；label 统一加 "段:" 前缀，
  结果并入合并 signals 列表，并暴露分组键 xd_beichi / xd_b23。
"""
from __future__ import annotations


def ema(vals: list[float], n: int) -> list[float]:
    """首值种子 EMA：alpha=2/(n+1)，首值种子为第一个样本。"""
    alpha = 2.0 / (n + 1)
    out = [float(vals[0])]
    for v in vals[1:]:
        out.append(alpha * float(v) + (1 - alpha) * out[-1])
    return out


def macd(closes: list[float]) -> tuple[list[float], list[float], list[float]]:
    """返回 (dif, dea, hist)，hist = 2*(DIF-DEA)（A股软件通行口径）。"""
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema(dif, 9)
    hist = [2.0 * (d - e) for d, e in zip(dif, dea)]
    return dif, dea, hist


def seg_area(hist: list[float], x0: int, x1: int, direction: str) -> float:
    """区间 (x0, x1] 内同向 hist 面积。"""
    s = 0.0
    for k in range(x0 + 1, x1 + 1):
        h = hist[k]
        if direction == "down" and h < 0:
            s += -h
        elif direction == "up" and h > 0:
            s += h
    return s


def detect_beichi_v2(bis: list[dict], hist: list[float], dif: list[float],
                     xds: list[dict]) -> list[dict]:
    """v2 背驰：段内纪录极值锚点；价格新极值 且（面积缩小 或 终点 DIF 衰竭）。

    返回 [{x, dt, price, side, label, cond, fallback, anchor_x, anchor_dt,
           anchor_price, area_cur, area_prev, area_ratio, exhaustion_pct,
           dif_cur, dif_prev}]
    area_ratio = 当前面积/锚点面积、exhaustion_pct = (1-area_ratio)*100，
    仅面积命中（cond in {area, both}）时有值，DIF 命中为 None。
    """
    label_map = {"down": "B1a", "up": "S1"}
    hits = []
    rec = {}        # 当前线段内 direction -> 纪录极值笔
    prev_same = {}  # direction -> 相邻前一同向笔（fallback 锚点）
    xi = -1         # 当前线段下标
    for b in bis:
        while xi + 1 < len(xds) and b["x0"] >= xds[xi + 1]["x0"]:
            xi += 1
            rec = {}
        in_seg = 0 <= xi and xds[xi]["x0"] <= b["x0"] and b["x1"] <= xds[xi]["x1"]

        d = b["direction"]
        area = seg_area(hist, b["x0"], b["x1"], d)
        dif_end = dif[b["x1"]]
        b["_area"] = area
        b["_dif_end"] = dif_end

        if in_seg:
            anchor, fallback = rec.get(d), False
        else:
            anchor, fallback = prev_same.get(d), True

        if anchor is not None:
            new_extreme = (b["y1"] < anchor["y1"]) if d == "down" else (b["y1"] > anchor["y1"])
            area_ok = area < anchor["_area"]
            dif_ok = (dif_end > anchor["_dif_end"]) if d == "down" else (dif_end < anchor["_dif_end"])
            if new_extreme and (area_ok or dif_ok):
                cond = "both" if (area_ok and dif_ok) else ("area" if area_ok else "dif")
                # 衰竭度仅面积命中时有意义；DIF 命中（面积未缩）为 None
                if area_ok and anchor["_area"] > 0:
                    ratio = round(area / anchor["_area"], 4)
                    exhaustion = round((1 - area / anchor["_area"]) * 100, 1)
                else:
                    ratio = exhaustion = None
                hits.append({
                    "x": b["x1"], "dt": b["dt1"], "price": b["y1"],
                    "side": "buy" if d == "down" else "sell",
                    "label": label_map[d] + ("-d" if cond == "dif" else ""),
                    "cond": cond,
                    "fallback": fallback,
                    "anchor_x": anchor["x1"],
                    "anchor_dt": anchor["dt1"], "anchor_price": anchor["y1"],
                    "area_cur": round(area, 2), "area_prev": round(anchor["_area"], 2),
                    "area_ratio": ratio, "exhaustion_pct": exhaustion,
                    "dif_cur": round(dif_end, 3), "dif_prev": round(anchor["_dif_end"], 3),
                    "forming": False,
                })

        prev_same[d] = b
        if in_seg:
            r = rec.get(d)
            if r is None or (d == "down" and b["y1"] < r["y1"]) or (d == "up" and b["y1"] > r["y1"]):
                rec[d] = b
    return hits


def detect_b3_s3(bis: list[dict], zss: list[dict]) -> list[dict]:
    """三买/三卖：中枢之后离开段突破 ZG/ZD，回抽笔不碰中枢。

    B3：向上笔 max(y0,y1) > ZG 离开后，下一根向下回抽笔 min(y0,y1) > ZG；
    S3 镜像。每个中枢每个方向至多取第一个。
    """
    points: list[dict] = []
    n = len(bis)

    def scan(x1, direction, cmp_leave, cmp_pull):
        for i in range(n):
            b = bis[i]
            if b["x0"] < x1:
                continue
            if b["direction"] == direction and cmp_leave(b):
                for j in range(i + 1, n):
                    b2 = bis[j]
                    if b2["direction"] != direction:
                        if cmp_pull(b2):
                            return b2, bis[i]
                        return None
                return None
        return None

    for zs in zss:
        zg, zd = zs["zg"], zs["zd"]

        r3 = scan(
            zs["x1"], "up",
            lambda b, _zg=zg: max(b["y0"], b["y1"]) > _zg,
            lambda b, _zg=zg: min(b["y0"], b["y1"]) > _zg,
        )
        if r3 is not None:
            pull, leave = r3
            points.append({
                "x": pull["x1"], "dt": pull["dt1"], "price": pull["y1"],
                "side": "buy", "label": "B3", "forming": False,
                "zs_dt0": zs["dt0"], "zs_dt1": zs["dt1"], "zg": zg, "zd": zd,
                "leave_dt": leave["dt1"], "leave_price": leave["y1"],
                "pull_price": pull["y1"],
            })

        r3s = scan(
            zs["x1"], "down",
            lambda b, _zd=zd: min(b["y0"], b["y1"]) < _zd,
            lambda b, _zd=zd: max(b["y0"], b["y1"]) < _zd,
        )
        if r3s is not None:
            pull, leave = r3s
            points.append({
                "x": pull["x1"], "dt": pull["dt1"], "price": pull["y1"],
                "side": "sell", "label": "S3", "forming": False,
                "zs_dt0": zs["dt0"], "zs_dt1": zs["dt1"], "zg": zg, "zd": zd,
                "leave_dt": leave["dt1"], "leave_price": leave["y1"],
                "pull_price": pull["y1"],
            })
    return points


def detect_b2_s2(bis: list[dict], beichi_hits: list[dict]) -> list[dict]:
    """二买/二卖：B1a/S1 之后第一根回抽笔不破前低/前高。

    B2：B1a（向下笔终点，买点）之后第一根向上笔之后的向下回抽笔，终点 > B1a 低点；
    S2 镜像。每个 B1a/S1 至多产生一个 B2/S2。
    """
    points: list[dict] = []
    for h in beichi_hits:
        # 找触发笔
        i0 = next((i for i, b in enumerate(bis) if b["x1"] == h["x"]), None)
        if i0 is None:
            continue
        # 之后第一根反向笔 + 再一根同向回抽笔
        if i0 + 2 >= len(bis):
            continue
        pull = bis[i0 + 2]
        if h["side"] == "buy":
            if pull["direction"] == "down" and pull["y1"] > h["price"]:
                points.append({
                    "x": pull["x1"], "dt": pull["dt1"], "price": pull["y1"],
                    "side": "buy", "label": "B2", "forming": False,
                    "anchor_dt": h["dt"], "anchor_price": h["price"],
                    "pull_price": pull["y1"],
                })
        else:
            if pull["direction"] == "up" and pull["y1"] < h["price"]:
                points.append({
                    "x": pull["x1"], "dt": pull["dt1"], "price": pull["y1"],
                    "side": "sell", "label": "S2", "forming": False,
                    "anchor_dt": h["dt"], "anchor_price": h["price"],
                    "pull_price": pull["y1"],
                })
    return points


def detect_forming(bis: list[dict], bars: list[dict]) -> dict | None:
    """最新未完成笔：最后确认笔终点之后，沿反方向走到区间内极值。

    最后确认笔为向下（终点低）→ 雏形向上，终点=其后最高价所在 bar；反之取最低。
    需要终点之后至少 1 根 K 线。
    """
    if not bis or len(bars) < 2:
        return None
    last = bis[-1]
    x0 = last["x1"]
    if x0 + 1 >= len(bars):
        return None
    if last["direction"] == "down":
        direction = "up"
        xi = max(range(x0 + 1, len(bars)), key=lambda k: bars[k]["high"])
        y1 = bars[xi]["high"]
    else:
        direction = "down"
        xi = min(range(x0 + 1, len(bars)), key=lambda k: bars[k]["low"])
        y1 = bars[xi]["low"]
    return {
        "x0": x0, "x1": xi,
        "y0": last["y1"], "y1": float(y1),
        "dt0": last["dt1"], "dt1": bars[xi]["dt"],
        "direction": direction, "forming": True,
    }


def detect_forming_signal(bis: list[dict], bars: list[dict],
                          hist: list[float], dif: list[float],
                          xds: list[dict] | None = None) -> dict | None:
    """雏形买卖点预判：对最新未完成笔，假设最新 bar 即笔终点，走 v2 同一判定。

    最新未完成笔两种形态：
    - detect_forming 给出雏形笔（最后确认笔终点之后的反向运动）→ 以它为预判笔；
    - 否则若末笔终点就是最后一根 bar（端点即极值，笔仍在行进，chanlun 已把它
      纳入笔序列但尚无反向笔确认）→ 末笔自身即未完成笔，替换末端重判。
    触发（价格新极值 + 面积或 DIF 衰竭，口径同 detect_beichi_v2）则输出
    「若当前笔在此终结将触发的信号」，label 加 "~" 前缀；否则 None。
    """
    if not bis or len(bars) < 2:
        return None
    forming = detect_forming(bis, bars)
    replace_last = False
    if forming is None:
        if bis[-1]["x1"] != len(bars) - 1:
            return None
        forming = bis[-1]
        replace_last = True
    xi = len(bars) - 1
    if xi <= forming["x0"]:
        return None
    d = forming["direction"]
    hypo = {
        "x0": forming["x0"], "x1": xi,
        "y0": forming["y0"],
        "y1": float(bars[xi]["high"] if d == "up" else bars[xi]["low"]),
        "dt0": forming["dt0"], "dt1": bars[xi]["dt"],
        "direction": d,
    }
    base = [dict(b) for b in (bis[:-1] if replace_last else bis)]
    hits = detect_beichi_v2(base + [hypo], hist, dif, xds or [])
    hit = next((h for h in hits if h["x"] == xi), None)
    if hit is None:
        return None
    return {
        "x": hit["x"], "dt": hit["dt"], "price": hit["price"],
        "side": hit["side"], "label": "~" + hit["label"],
        "cond": hit["cond"], "forming": True,
        "anchor": {"dt": hit["anchor_dt"], "price": hit["anchor_price"]},
        "area_pair": [hit["area_cur"], hit["area_prev"]],
        "dif_pair": [hit["dif_cur"], hit["dif_prev"]],
        "note": "雏形：若当前笔在最新价处终结将触发 " + hit["label"]
                + "（未经反向笔确认）",
    }


def _prefixed(hits: list[dict], prefix: str) -> list[dict]:
    """给信号 label 加级别前缀（段级信号统一 "段:" 前缀）。"""
    for h in hits:
        h["label"] = prefix + h["label"]
    return hits


def compute_signals(bars: list[dict], structure: dict) -> dict:
    """汇总：MACD + 全部买卖点 + 雏形笔；含段级（xd 当 bi、zs_xd 当 zs）分支。"""
    closes = [b["close"] for b in bars]
    dif, dea, hist = macd(closes)
    bis, xds, zss = structure["bi"], structure["xd"], structure["zs"]
    zs_xd = structure.get("zs_xd", [])

    beichi = detect_beichi_v2(bis, hist, dif, xds)
    b3s3 = detect_b3_s3(bis, zss)
    b2s2 = detect_b2_s2(bis, beichi)
    forming = detect_forming(bis, bars)
    forming_signal = detect_forming_signal(bis, bars, hist, dif, xds)

    # 段级：xd 当 bi 复用同一组检测函数；xd 无上级线段，背驰锚点即相邻同向线段
    # （v2 fallback 路径）。forming 线段不参与检测；复制 dict 避免 _area 等临时键
    # 污染 structure 输出。
    xd_seq = [dict(x) for x in xds if not x.get("forming")]
    xd_beichi = _prefixed(detect_beichi_v2(xd_seq, hist, dif, []), "段:")
    xd_b23 = _prefixed(
        detect_b3_s3(xd_seq, zs_xd) + detect_b2_s2(xd_seq, xd_beichi), "段:")

    signals = sorted(beichi + b3s3 + b2s2 + xd_beichi + xd_b23,
                     key=lambda s: s["x"])
    return {
        "macd": {"dif": dif, "dea": dea, "hist": hist},
        "signals": signals,
        "xd_beichi": xd_beichi,
        "xd_b23": xd_b23,
        "forming": forming,
        "forming_signal": forming_signal,
    }
