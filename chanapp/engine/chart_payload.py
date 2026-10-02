"""Chart assembly and per-timeframe summaries, isolated by data and calculation identity."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable

from chanapp.engine import chanpy_adapter
from chanapp.engine import period_preferences
from chanapp.engine import channels as engine_channels
from chanapp.engine import compute_cache as engine_compute_cache
from chanapp.engine import data as engine_data
from chanapp.engine.chanpy_profiles import profile_identity

log = logging.getLogger(__name__)

RESONANCE_FREQS = ("day", "m60", "m30")


def analysis_periods(code: str) -> tuple[str, ...]:
    """日线必需，分钟按偏好与市场能力参与；顺序也是共振与 AI 的输入顺序。"""
    prefs = period_preferences.current()
    if prefs is None:
        return RESONANCE_FREQS
    minutes = prefs.allowed(code, RESONANCE_FREQS[1:])  # 一次取齐，避免并发保存拼出从未存在的组合
    return ("day",) + minutes


def analysis_calculation_id(calculation_id: str, freqs) -> str:
    """组合计算身份；单周期结构身份不变，仍可跨组合复用。"""
    return hashlib.sha256(json.dumps([calculation_id, list(freqs)]).encode()).hexdigest()


def read_bundle(code: str, adjust: str = "qfq", get_bars_fn: Callable | None = None) -> dict:
    """{参与周期: 数据集或 None}。日线及已选可合成分钟出自同一次读事务（AI 走它，
    主图走 read_chart_inputs）；否则（公开演示门面或显式给 get_bars_fn）逐周期读取。标的没有任何事实或单周期
    读取失败为 None；视图存在而无 bar 时为空 bars 的数据集，令牌仍是当前视图身份。"""
    freqs = analysis_periods(code)
    bundle_fn = None if get_bars_fn else getattr(engine_data, "get_bars_bundle", None)
    if bundle_fn is not None:
        try:
            return dict.fromkeys(freqs) | bundle_fn(code, freqs, adjust=adjust)
        except Exception:
            log.warning("共振数据读取失败 code=%s", code, exc_info=True)
            return dict.fromkeys(freqs)
    out = dict.fromkeys(freqs)
    for freq in freqs:
        try:
            out[freq] = (get_bars_fn or engine_data.get_bars)(code, freq, adjust=adjust)
        except Exception:
            log.warning("共振级别取数失败 code=%s freq=%s", code, freq, exc_info=True)
            out[freq] = None
    return out


def _unsupported(dataset: dict) -> bool:
    return any(n.get("code") == "unsupported" for n in dataset.get("notices") or [])


def is_structure_short(dataset: dict | None) -> bool:
    """结构输入不足默认窗口（门面带 structure_short 提示）：结论只能作为暂定。"""
    return any(n.get("code") == "structure_short" for n in (dataset or {}).get("notices") or [])


def read_chart_inputs(code: str, freq: str, adjust: str = "qfq") -> tuple:
    """(主图数据集, 共振 bundle)：门面有 get_bars_bundle 时当前周期与可用共振输入一次读取（spec §6.3），
    主图令牌与 analysis_tokens 因此同属一个版本。当前周期在 bundle 里缺席（公开演示门面或替身）时回落
    get_bars。只有当前周期的首取失败上抛，共振周期首取失败时该周期缺省（primary=freq）。
    主图无数据且不是「该市场暂不提供」时抛 LookupError，异常由调用方转 502。"""
    bundle_fn = getattr(engine_data, "get_bars_bundle", None)
    analysis_freqs = analysis_periods(code)
    freqs = analysis_freqs + ((freq,) if freq not in analysis_freqs else ())
    bundle = (dict(bundle_fn(code, freqs, adjust=adjust, primary=freq)) if bundle_fn is not None
              else read_bundle(code, adjust))
    dataset = bundle.get(freq)
    if dataset is None:
        dataset = engine_data.get_bars(code, freq, adjust=adjust)
        if freq in RESONANCE_FREQS:
            bundle[freq] = dataset
    if not dataset.get("bars") and not _unsupported(dataset):
        raise LookupError(f"{code} {freq} 暂无可服务数据")
    return dataset, {f: bundle.get(f) for f in analysis_freqs}


def _record(code: str, freq: str, bars: list, data_version: str, calculation_id: str, signals: list) -> None:
    try:
        record_fn = getattr(engine_data, "record_calc_run", None)
        if record_fn is not None and bars:
            record_fn(code, freq, input_start=bars[0]["dt"], input_end=bars[-1]["dt"],
                      input_data_version=data_version, calculation_id=calculation_id, signals=signals)
    except Exception:
        log.warning("结论历史记录失败 code=%s freq=%s", code, freq, exc_info=True)


def _level_summary(code: str, freq: str, dataset: dict | None,
                   rule_profile: str = "strict", signal_scope: str = "expanded") -> dict | None:
    """单周期摘要：最新笔/段原生点位及状态、最新中枢。

    优先复用 compute_cache（完整数据版本一致才命中），否则现算并回填；
    无数据或计算失败返回 None（该级角标缺省）。
    """
    identity = profile_identity(rule_profile, signal_scope)
    try:
        bars = (dataset or {}).get("bars")
        if not bars:
            return None
        data_version = engine_compute_cache.dataset_version(dataset)
        cached = engine_compute_cache.get(code, freq, data_version, identity["calculation_id"])
        if cached is not None:
            structure, sig = cached["structure"], cached["sig"]
        else:
            result = chanpy_adapter.compute_analysis(bars, freq, rule_profile=rule_profile, signal_scope=signal_scope)
            structure, sig, evidence = result["structure"], result["sig"], result["evidence"]
            engine_compute_cache.put(code, freq, data_version, structure,
                                     sig, evidence, calculation_id=identity["calculation_id"])
        latest = {s["level"]: s for s in sig["signals"]}
        zs = structure["zs"][-1] if structure["zs"] else None
        _record(code, freq, bars, data_version, identity["calculation_id"], sig["signals"])
        close = bars[-1]["close"]
        return {
            "freq": freq, **identity,
            "structure_short": is_structure_short(dataset),
            "signals": [latest[level] for level in ("bi", "seg") if level in latest],
            "zs": ({"zg": zs["zg"], "zd": zs["zd"],
                    "inside": bool(zs["zd"] <= close <= zs["zg"])}
                   if zs else None),
        }
    except Exception:
        log.warning("共振级别摘要失败 code=%s freq=%s", code, freq, exc_info=True)
        return None


def _resonance(code: str, bundle: dict, rule_profile: str = "strict", signal_scope: str = "expanded") -> list[dict]:
    """参与级别摘要顺序执行（本地读，线程池在 GIL 下无收益）；单级缺数据或失败 → 该级缺省。"""
    summaries = [_level_summary(code, freq, bundle.get(freq), rule_profile, signal_scope)
                 for freq in RESONANCE_FREQS]
    return [s for s in summaries if s is not None]


def build_chart_payload(code: str, freq: str, dataset: dict | None = None,
                        get_bars_fn: Callable | None = None,
                        timings: dict | None = None, rule_profile: str = "strict", signal_scope: str = "expanded",
                        *, adjust: str = "qfq", bundle: dict | None = None) -> dict:
    """组装 chart 响应体（与 /api/chart 返回逐字段一致）。

    dataset 为 None 时调 get_bars_fn（未给则 engine.data.get_bars）现取；bundle 为 None 时经 read_bundle
    读本次分析组合。异常不捕获，由调用方处理。timings 给 dict 时回填 compute_ms/resonance_ms。
    """
    identity = profile_identity(rule_profile, signal_scope)
    if dataset is None:
        dataset = (get_bars_fn or engine_data.get_bars)(code, freq, adjust=adjust)
    t0 = time.monotonic()

    bars = dataset["bars"]
    data_version = engine_compute_cache.dataset_version(dataset)
    cached = engine_compute_cache.get(code, freq, data_version, identity["calculation_id"])
    if cached is None:
        result = chanpy_adapter.compute_analysis(bars, freq, rule_profile=rule_profile, signal_scope=signal_scope)
        structure, sig, evidence = result["structure"], result["sig"], result["evidence"]
        engine_compute_cache.put(code, freq, data_version, structure, sig, evidence,
                                 calculation_id=identity["calculation_id"])
    else:
        structure, sig, evidence = cached["structure"], cached["sig"], cached["evidence"]
    # 结论历史挂在发布点（非 compute_cache），幂等键去重使重复记录零成本（spec §2.7）。
    _record(code, freq, bars, data_version, identity["calculation_id"], sig["signals"])
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

    meta = {k: v for k, v in dataset.items() if k not in ("bars",)}
    meta["data_version"] = data_version
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

    if bundle is None:
        bundle = read_bundle(code, adjust, get_bars_fn)
    resonance = _resonance(code, bundle, rule_profile, signal_scope)
    # AI 请求所需令牌的唯一来源：与共振同一次读取、同一复权模式，与当前查看的周期无关
    freqs = tuple(f for f in RESONANCE_FREQS if f in bundle)
    meta["analysis_freqs"] = list(freqs)
    meta["analysis_calculation_id"] = analysis_calculation_id(identity["calculation_id"], freqs)
    meta["analysis_tokens"] = {f: (bundle.get(f) or {}).get("token") for f in freqs}
    t_res = time.monotonic()
    if timings is not None:
        timings["compute_ms"] = int((t_compute - t0) * 1000)
        timings["resonance_ms"] = int((t_res - t_compute) * 1000)
    return {
        **identity,
        "kline": kline,
        "macd": {"rows": macd_rows},
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
