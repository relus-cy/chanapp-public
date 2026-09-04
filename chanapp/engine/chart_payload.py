"""chart 响应体组装器：/api/chart 与 Pages 推送平面（scripts/fetcher/pages_push.py）共用。

build_chart_payload(code, freq, dataset=None, get_bars_fn=None, timings=None)：
dataset 为 None 时现取（get_bars_fn 未给则走 engine.data.get_bars 门面，运行时解析、
可被 mock.patch 替换）；取数/计算异常不在此转换，由调用方决定（api 层转 502，
推送侧记日志跳过）。返回 dict 与 /api/chart 响应逐字段一致；compute_cache.put
副作用与原 api_chart 相同（末 bar dt 一致时共振 _level_summary 命中缓存）。

resonance：多级别共振角标（日/60/30 三级各取最新确认信号、最新中枢、雏形信号），
纯计算零新数据，复用 compute_cache；某级取数/计算失败则该级缺省，不拖垮主响应。

timings：可选 out-param dict，填入 compute_ms / resonance_ms 两段耗时（api 层
[timing] 日志用；取数段由调用方自测）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from chanapp.engine import channels as engine_channels
from chanapp.engine import compute_cache as engine_compute_cache
from chanapp.engine import data as engine_data
from chanapp.engine import evidence as engine_evidence
from chanapp.engine import signals as engine_signals
from chanapp.engine import structure as engine_structure

log = logging.getLogger(__name__)

RESONANCE_FREQS = ("day", "m60", "m30")


def _level_summary(code: str, freq: str,
                   get_bars_fn: Callable | None = None) -> dict | None:
    """单级别摘要：最新确认信号 / 最新中枢（含末收是否在中枢内）/ 雏形信号。

    优先复用 compute_cache（末 bar dt 一致才命中），否则现算并回填；
    取数或计算失败返回 None（该级角标缺省）。
    """
    try:
        dataset = (get_bars_fn or engine_data.get_bars)(code, freq)
        bars = dataset["bars"]
        if not bars:
            return None
        cached = engine_compute_cache.get(code, freq, bars[-1]["dt"])
        if cached is not None:
            structure, sig = cached["structure"], cached["sig"]
        else:
            structure = engine_structure.compute_structure(bars, code, freq)
            sig = engine_signals.compute_signals(bars, structure)
            # evidence 必须一并算好：/api/analysis 命中同一缓存时直接复用
            evidence = engine_evidence.build_evidence(sig["signals"], structure)
            engine_compute_cache.put(code, freq, bars[-1]["dt"], structure,
                                     sig, evidence)
        latest_sig = sig["signals"][-1] if sig["signals"] else None
        zs = structure["zs"][-1] if structure["zs"] else None
        close = bars[-1]["close"]
        return {
            "freq": freq,
            "signal": ({"label": latest_sig["label"], "dt": latest_sig["dt"]}
                       if latest_sig else None),
            "zs": ({"zg": zs["zg"], "zd": zs["zd"],
                    "inside": bool(zs["zd"] <= close <= zs["zg"])}
                   if zs else None),
            "forming_signal": ({"label": sig["forming_signal"]["label"],
                                "dt": sig["forming_signal"]["dt"]}
                               if sig["forming_signal"] else None),
        }
    except Exception:
        log.warning("共振级别摘要失败 code=%s freq=%s", code, freq, exc_info=True)
        return None


def _resonance(code: str, get_bars_fn: Callable | None = None) -> list[dict]:
    """三级别并行摘要（pool.map 保序）；单级失败返回 None → 该级缺省，不拖垮主响应。"""
    with ThreadPoolExecutor(max_workers=len(RESONANCE_FREQS),
                            thread_name_prefix="resonance") as pool:
        summaries = list(pool.map(lambda f: _level_summary(code, f, get_bars_fn),
                                  RESONANCE_FREQS))
    return [s for s in summaries if s is not None]


def build_chart_payload(code: str, freq: str, dataset: dict | None = None,
                        get_bars_fn: Callable | None = None,
                        timings: dict | None = None) -> dict:
    """组装 chart 响应体（与 /api/chart 返回逐字段一致）。

    dataset 为 None 时调 get_bars_fn（未给则 engine.data.get_bars）现取；
    异常不捕获，由调用方处理。timings 给 dict 时回填 compute_ms/resonance_ms。
    """
    if dataset is None:
        dataset = (get_bars_fn or engine_data.get_bars)(code, freq)
    t0 = time.monotonic()

    bars = dataset["bars"]
    structure = engine_structure.compute_structure(bars, code, freq)
    sig = engine_signals.compute_signals(bars, structure)
    evidence = engine_evidence.build_evidence(sig["signals"], structure)
    engine_compute_cache.put(code, freq, bars[-1]["dt"], structure, sig, evidence)
    t_compute = time.monotonic()

    kline = [
        {"time": b["dt"], "open": b["open"], "high": b["high"], "low": b["low"],
         "close": b["close"], "volume": b["volume"]}
        for b in bars
    ]
    macd = sig["macd"]
    macd_rows = [
        {"time": b["dt"], "dif": round(macd["dif"][i], 4),
         "dea": round(macd["dea"][i], 4), "hist": round(macd["hist"][i], 4)}
        for i, b in enumerate(bars)
    ]
    # 背驰连线：锚点 bar → 信号 bar 的 DIF 连线（MACD 副图虚线用）
    beichi_links = [
        {"x0": s["anchor_x"], "x1": s["x"],
         "dif0": round(macd["dif"][s["anchor_x"]], 4),
         "dif1": round(macd["dif"][s["x"]], 4),
         "label": s["label"]}
        for s in sig["signals"] if "anchor_x" in s
    ]

    meta = {k: v for k, v in dataset.items() if k not in ("bars",)}
    meta["bars"] = len(bars)
    meta["first_dt"] = bars[0]["dt"]
    meta["last_dt"] = bars[-1]["dt"]

    # 通道线：x 索引转时间（x1 已延伸到最后已知 bar，恒在 bars 范围内）
    def _rail_out(r: dict) -> dict:
        return {"t0": bars[r["x0"]]["dt"], "y0": round(r["y0"], 4),
                "t1": bars[r["x1"]]["dt"], "y1": round(r["y1"], 4)}

    channels = [
        {"upper": _rail_out(ch["upper"]), "lower": _rail_out(ch["lower"]),
         "direction": ch["direction"]}
        for ch in engine_channels.build(structure["bi"], structure["xd"])
    ]

    resonance = _resonance(code, get_bars_fn)
    t_res = time.monotonic()
    if timings is not None:
        timings["compute_ms"] = int((t_compute - t0) * 1000)
        timings["resonance_ms"] = int((t_res - t_compute) * 1000)
    return {
        "kline": kline,
        "macd": {"rows": macd_rows, "beichi_links": beichi_links},
        "structure": {
            "bi": structure["bi"],
            "xd": structure["xd"],
            "zs": structure["zs"],
            "forming": sig["forming"],
            "counts": structure["counts"],
        },
        "signals": sig["signals"],
        "forming_signal": sig["forming_signal"],
        "evidence": evidence,
        "channels": channels,
        "resonance": resonance,
        "meta": meta,
    }
