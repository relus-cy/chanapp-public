"""Chart assembly and per-timeframe summaries, isolated by data and calculation identity."""
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
from chanapp.engine import supply
from chanapp.engine.chanpy_profiles import profile_identity

log = logging.getLogger(__name__)

RESONANCE_FREQS = ("day", "m60", "m30")


def _level_summary(code: str, freq: str,
                   get_bars_fn: Callable | None = None, rule_profile: str = "strict") -> dict | None:
    """单周期摘要：最新笔/段原生点位及状态、最新中枢。

    优先复用 compute_cache（完整数据版本一致才命中），否则现算并回填；
    取数或计算失败返回 None（该级角标缺省）。
    """
    identity = profile_identity(rule_profile)
    try:
        dataset = (get_bars_fn or engine_data.get_bars)(code, freq)
        bars = dataset["bars"]
        if not bars:
            return None
        data_version = engine_compute_cache.dataset_version(dataset)
        cached = engine_compute_cache.get(code, freq, data_version, identity["calculation_id"])
        if cached is not None:
            structure, sig = cached["structure"], cached["sig"]
        else:
            structure = engine_structure.compute_structure(bars, code, freq, rule_profile=rule_profile)
            sig = engine_signals.compute_signals(bars, structure)
            # evidence 必须一并算好：/api/analysis 命中同一缓存时直接复用
            evidence = engine_evidence.build_evidence(sig["signals"], structure)
            engine_compute_cache.put(code, freq, data_version, structure,
                                     sig, evidence, calculation_id=identity["calculation_id"])
        latest = {s["level"]: s for s in sig["signals"]}
        zs = structure["zs"][-1] if structure["zs"] else None
        close = bars[-1]["close"]
        return {
            "freq": freq, **identity,
            "signals": [latest[level] for level in ("bi", "seg") if level in latest],
            "zs": ({"zg": zs["zg"], "zd": zs["zd"],
                    "inside": bool(zs["zd"] <= close <= zs["zg"])}
                   if zs else None),
        }
    except Exception:
        log.warning("共振级别摘要失败 code=%s freq=%s", code, freq, exc_info=True)
        return None


def _resonance(code: str, get_bars_fn: Callable | None = None, rule_profile: str = "strict") -> list[dict]:
    """三级别并行摘要（pool.map 保序）；单级失败返回 None → 该级缺省，不拖垮主响应。"""
    snapshot = supply.current()
    permit = supply.capture_write_permit()

    def summarize(freq):
        with supply.use(snapshot, permit):
            return _level_summary(code, freq, get_bars_fn, rule_profile)

    with ThreadPoolExecutor(max_workers=len(RESONANCE_FREQS),
                            thread_name_prefix="resonance") as pool:
        summaries = list(pool.map(summarize,
                                  RESONANCE_FREQS))
    return [s for s in summaries if s is not None]


def build_chart_payload(code: str, freq: str, dataset: dict | None = None,
                        get_bars_fn: Callable | None = None,
                        timings: dict | None = None, rule_profile: str = "strict") -> dict:
    """组装 chart 响应体（与 /api/chart 返回逐字段一致）。

    dataset 为 None 时调 get_bars_fn（未给则 engine.data.get_bars）现取；
    异常不捕获，由调用方处理。timings 给 dict 时回填 compute_ms/resonance_ms。
    """
    identity = profile_identity(rule_profile)
    snapshot = supply.current()
    if dataset is None:
        with supply.use(snapshot):
            dataset = (get_bars_fn or engine_data.get_bars)(code, freq)
    t0 = time.monotonic()

    bars = dataset["bars"]
    with supply.use(snapshot):
        data_version = engine_compute_cache.dataset_version(dataset)
    cached = engine_compute_cache.get(code, freq, data_version, identity["calculation_id"])
    if cached is None:
        structure = engine_structure.compute_structure(bars, code, freq, rule_profile=rule_profile)
        sig = engine_signals.compute_signals(bars, structure)
        evidence = engine_evidence.build_evidence(sig["signals"], structure)
        engine_compute_cache.put(code, freq, data_version, structure, sig, evidence,
                                 calculation_id=identity["calculation_id"])
    else:
        structure, sig, evidence = cached["structure"], cached["sig"], cached["evidence"]
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
    # No synthetic links for native morphology points.
    beichi_links = []

    meta = {k: v for k, v in dataset.items() if k not in ("bars",)}
    meta.update(scheme=snapshot.scheme, generation=snapshot.generation, epoch=snapshot.epoch, data_version=data_version)
    meta["bars"] = len(bars)
    meta["first_dt"] = bars[0]["dt"] if bars else None
    meta["last_dt"] = bars[-1]["dt"] if bars else None

    # 通道线：x 索引转时间（x1 已延伸到最后已知 bar，恒在 bars 范围内）
    def _rail_out(r: dict) -> dict:
        return {"t0": bars[r["x0"]]["dt"], "y0": round(r["y0"], 4),
                "t1": bars[r["x1"]]["dt"], "y1": round(r["y1"], 4)}

    channels = [
        {"upper": _rail_out(ch["upper"]), "lower": _rail_out(ch["lower"]),
         "direction": ch["direction"]}
        for ch in engine_channels.build(structure["bi"], structure["xd"])
    ]

    with supply.use(snapshot):
        resonance = _resonance(code, get_bars_fn, rule_profile)
    t_res = time.monotonic()
    if timings is not None:
        timings["compute_ms"] = int((t_compute - t0) * 1000)
        timings["resonance_ms"] = int((t_res - t_compute) * 1000)
    return {
        **identity,
        "scheme": snapshot.scheme,
        "generation": snapshot.generation, "epoch": snapshot.epoch,
        "kline": kline,
        "macd": {"rows": macd_rows, "beichi_links": beichi_links},
        "structure": {
            "bi": structure["bi"],
            "xd": structure["xd"],
            "zs": structure["zs"],
            "zs_xd": structure.get("zs_xd", []),
            "forming": sig["forming"],
            "counts": structure["counts"],
        },
        "signals": sig["signals"],
        "evidence": evidence,
        "channels": channels,
        "resonance": resonance,
        "meta": meta,
    }
