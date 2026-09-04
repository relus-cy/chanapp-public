"""chanlun 缠论结构计算：缠K/分型/笔/线段/笔中枢。

口径（与 tmp/chan-validation 已验证脚本一致）：
- 观察者周期秒数：日线=86400，周线=604800，30分=1800，60分=3600；
- 逐根投喂 OHLCV（时间戳为本地时区当日 0 点，仅作内部对齐）；
- 笔/线段端点归一化为 {x0,x1,y0,y1,dt0,dt1,direction}（x 为 K 线索引，y 为分型特征值）；
- 中枢归一化为 {x0,x1,dt0,dt1,zg,zd,gg,dd}。

chanlun（pip 包，Rust 核心）只提供结构，不提供买卖点；买卖点见 signals.py。
"""
from __future__ import annotations

from datetime import datetime

from chanlun import 观察者, 缠论配置

FREQ_SECONDS = {"day": 86400, "week": 7 * 86400, "m30": 1800, "m60": 3600,
                "m15": 900, "m5": 300}  # m15/m5 于 v1.4.1 放开（week 已废弃，留映射无害）


def _to_ts(dt: str) -> int:
    """'YYYY-MM-DD'（可能带时间）-> 本地时区时间戳。"""
    fmt = "%Y-%m-%d %H:%M" if " " in dt else "%Y-%m-%d"
    return int(datetime.strptime(dt, fmt).timestamp())


def _ts_str(ts: int, seconds: int = 86400) -> str:
    """分钟级周期保留时分；日线/周线只到日。"""
    fmt = "%Y-%m-%d %H:%M" if seconds < 86400 else "%Y-%m-%d"
    return datetime.fromtimestamp(ts).strftime(fmt)


def _forming_xd(bis: list[dict], xds: list[dict]) -> dict | None:
    """补最新未确认线段：上条线段终点之后，沿反向走到同向笔的极值端点。"""
    if not xds or not bis:
        return None
    last = xds[-1]
    direction = "up" if last["direction"] == "down" else "down"
    later = [b for b in bis if b["x1"] > last["x1"] and b["direction"] == direction]
    if not later:
        return None
    end = max(later, key=lambda b: b["y1"]) if direction == "up" \
        else min(later, key=lambda b: b["y1"])
    return {
        "x0": last["x1"], "x1": end["x1"],
        "y0": last["y1"], "y1": end["y1"],
        "dt0": last["dt1"], "dt1": end["dt1"],
        "direction": direction, "forming": True,
    }


def compute_structure(bars: list[dict], code: str, freq: str = "day") -> dict:
    """对 bars（按时间升序）计算缠论结构，返回 {bi, xd, zs, zs_xd, counts}。"""
    seconds = FREQ_SECONDS.get(freq)
    if seconds is None:
        raise ValueError(f"unsupported freq: {freq}")

    dts = [b["dt"] for b in bars]
    ts_to_idx = {_to_ts(d): i for i, d in enumerate(dts)}

    obs = 观察者(code, seconds, 缠论配置())
    for b in bars:
        obs.投喂原始数据(
            _to_ts(b["dt"]), b["open"], b["high"], b["low"], b["close"], b["volume"]
        )

    def norm_seg(obj) -> dict | None:
        """笔/线段统一：端点 (dt, 价格, 方向)。"""
        d0, d1 = _ts_str(obj.文.时间戳, seconds), _ts_str(obj.武.时间戳, seconds)
        x0 = ts_to_idx.get(_to_ts(d0))
        x1 = ts_to_idx.get(_to_ts(d1))
        if x0 is None or x1 is None:
            return None
        return {
            "x0": x0,
            "x1": x1,
            "y0": float(obj.文.分型特征值),
            "y1": float(obj.武.分型特征值),
            "dt0": d0,
            "dt1": d1,
            "direction": "up" if "向上" in str(obj.方向) else "down",
            "forming": False,
        }

    bis = sorted((s for s in (norm_seg(x) for x in obs.笔序列) if s), key=lambda b: b["x0"])
    xds = sorted((s for s in (norm_seg(x) for x in obs.线段序列) if s), key=lambda b: b["x0"])

    def norm_zs(seq) -> list[dict]:
        out = []
        for z in seq:
            x0 = ts_to_idx.get(z.文.时间戳)
            x1 = ts_to_idx.get(z.武.时间戳)
            if x0 is None or x1 is None:
                continue
            out.append(
                {
                    "x0": x0,
                    "x1": x1,
                    "dt0": _ts_str(z.文.时间戳, seconds),
                    "dt1": _ts_str(z.武.时间戳, seconds),
                    "zg": float(z.高),
                    "zd": float(z.低),
                    "gg": float(z.高高),
                    "dd": float(z.低低),
                }
            )
        out.sort(key=lambda z: z["x0"])
        return out

    zss = norm_zs(obs.笔_中枢序列)
    # 线段中枢：观察者「中枢序列」即由线段序列构成的中枢（zs_xd）
    zs_xd = norm_zs(obs.中枢序列)

    # chanlun 线段序列只含已确认线段；最新一段（上一条线段终点之后、尚未被反向线段
    # 确认）库不输出。这里按笔序列补一条 forming 线段：从上条线段终点出发，沿反向
    # 走到其后同向笔的极值端点。仅作展示，forming=True 与确认线段区分。
    xd_forming = _forming_xd(bis, xds)
    if xd_forming is not None:
        xds = xds + [xd_forming]

    return {
        "bi": bis,
        "xd": xds,
        "zs": zss,
        "zs_xd": zs_xd,
        "counts": {
            "cl_kline": len(obs.缠论K线序列),
            "fx": len(obs.分型序列),
            "bi": len(bis),
            "xd": len(xds),
            "bi_zs": len(zss),
            "xd_zs": len(zs_xd),
        },
    }
