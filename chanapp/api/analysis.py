"""AI 完全分类端点：GET /api/analysis?code=&freq=&adjust=&tokens=

联合日线及已选可合成分钟周期，一次读事务取齐（门面 bundle），与前端回传的主图 meta.analysis_tokens 逐周期比对，
任一不符或缺失返回 409 {"detail", "tokens": 当前令牌}，不调用模型（spec §6.3）；令牌校验先于判空，
令牌相符而某周期无数据才 502。输入未取够默认窗口的周期照常分析，prompt 与响应 structure_short 标出。

联合日线及已选可合成分钟周期，组装与 /api/chart 同源的结构数据（bars/structure/signals/evidence），
构造完全分类 prompt 调 LLM，按 prompt 文本哈希缓存结果：
- 缓存文件：<AI 缓存目录>/{sha256(prompt)}.json；目录为 ANALYSIS_CACHE_DIR，否则由实例目录派生，
  再否则为 chanapp/.cache/analysis（engine/instance_paths.py）。
- llm.is_configured() 为 False → 直接返回 status "unconfigured"，不取数不调 LLM。
- LLM 输出解析不成 JSON → 降级返回 raw 文本（status 仍 "ok"，scenarios 为空）。

Prompt 契约（build_prompt）：稳定指令与输出契约前置、数据 JSON 置尾（DeepSeek 前缀缓存友好）；
按周期分别喂 {code, freq, 最近中枢上沿/下沿, 最近 5 张依据卡（旧→新）, 形成中信号, 末bar dt/价}；
要求输出 JSON {"current_state", "scenarios"
[≤3, 各含 name/prior/evidence_for/evidence_against/posterior/trigger/boundary/
action/basis/update_watch]}，按贝叶斯结构推理：prior 先验、evidence_for/against
证据更新、posterior 后验置信（定性三档 高/中/低）、update_watch 更新观察点；
current_state 含一句「当前后验排序」。禁止输出概率数字；证据与 trigger/boundary
必须引用输入中出现过的信号 label 与价位（约束写进 prompt 文本，不做输出校验）。
"""
from __future__ import annotations

from typing import Annotated, Literal

import hashlib
import json
import logging
import re
import time
import threading
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from chanapp.api import view_log as api_view_log
from chanapp.engine import chanpy_adapter
from chanapp.engine import chart_payload as engine_chart_payload
from chanapp.engine import compute_cache as engine_compute_cache
from chanapp.engine import data as engine_data
from chanapp.engine import instance_paths
from chanapp.engine import llm as engine_llm
from chanapp.engine import data_identity
from chanapp.engine.chanpy_profiles import profile_identity


# LLM 失败条目（status="llm_error"）短 TTL：故障期内同一输入的重复请求（手动刷新或其他调用方）
# 直接 502，不再每次重调 LLM 并等满超时；过期后自动恢复重试。AI 分析只由手动触发，行情自动刷新不请求。
FAILURE_CACHE_TTL = 600

log = logging.getLogger(__name__)

router = APIRouter()

ANALYSIS_SCOPE_VERSION = "multi_timeframe_chanpy_v2"
_analysis_locks_guard = threading.Lock()
_analysis_locks: dict[str, list] = {}


@contextmanager
def _analysis_lock(key: str):
    """Coalesce identical in-flight analyses without retaining idle locks."""
    with _analysis_locks_guard:
        entry = _analysis_locks.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _analysis_locks_guard:
            entry[1] -= 1
            if not entry[1]:
                del _analysis_locks[key]



def _cache_dir() -> Path:
    # 请求时解析，测试可通过 ANALYSIS_CACHE_DIR 注入临时目录
    return instance_paths.current().analysis_dir


def _strip_pct(text: str) -> str:
    """依据卡文案去掉「衰竭度 N%」百分数（百分比数字不喂给 LLM）。"""
    return re.sub(r"，衰竭度\s*[\d.]+%", "", text)


def _round_floats(obj):
    """prompt 数值统一 4 位小数：消除浮点尾数噪声（同值不同字节会打掉缓存前缀）。"""
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, dict):
        return {k: _round_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v) for v in obj]
    return obj


# 形成中信号进 prompt 时剔除随时间窗滑动整体位移的内部索引字段
_PROV_DROP = ("x", "forming", "structure_ref", "last_sure_pos")


def _prompt_signal(s: dict) -> dict:
    out = {k: v for k, v in s.items() if k not in _PROV_DROP}
    rel = out.get("related_bsp1")
    if isinstance(rel, dict):
        out["related_bsp1"] = {k: rel.get(k) for k in ("dt", "price", "types")}
    return out


def collect_prompt_data(code: str, freq: str, bars: list[dict],
                        structure: dict, sig: dict,
                        evidence_cards: list[dict], *, structure_short: bool = False) -> dict:
    """从组装数据提取 prompt 输入。

    字段按稳定性排列（最易变的 last_bar 在末），依据卡旧→新（新卡尾部追加），
    让两次刷新间的 prompt 公共前缀尽可能长（DeepSeek 前缀缓存按前缀命中）。
    structure_short 只在输入不足默认窗口时出现（输入充足时 prompt 与原来逐字节一致）。
    """
    last = bars[-1]
    zss = structure.get("zs") or []
    zs = zss[-1] if zss else None
    cards = sorted(evidence_cards, key=lambda c: c["dt"])[-5:]
    short = {"structure_short": True} if structure_short else {}
    return _round_floats({
        "code": code,
        "freq": freq,
        **short,
        "latest_zs": ({"dt0": zs["dt0"], "dt1": zs["dt1"],
                       "中枢上沿": zs["zg"], "中枢下沿": zs["zd"]} if zs else None),
        "recent_evidence": [
            {"dt": c["dt"], "type": c["type"], "price": c["price"],
             "side": c["side"], "text": _strip_pct(c["text"]),
             "status": c.get("status", "provisional"), "level": c.get("level"),
             "types": c.get("types", []),
             "context": c.get("detail", {}).get("context"),
             "strength": c.get("detail", {}).get("strength")}
            for c in cards
        ],
        "provisional_signals": [_prompt_signal(s) for s in sig.get("signals", [])
                                if s.get("status") == "provisional"],
        "last_bar": {"dt": last["dt"], "close": last["close"]},
    })


def build_prompt(data: dict) -> str:
    """完全分类 prompt：固定指令 + 输出契约（稳定前缀）在前，数据 JSON 在末。

    DeepSeek 上下文缓存按前缀完全匹配命中；稳定部分全部前置后，
    两次刷新只有末尾数据段变化时可命中前缀缓存。
    """
    body = json.dumps(data, ensure_ascii=False, indent=2)
    freqs = tuple((data.get("timeframes") or {"day": data}).keys())
    labels = {"day": "日线(day)", "m60": "60分钟(m60)", "m30": "30分钟(m30)"}
    scope_note = (
        "联合日线(day)、60分钟(m60)、30分钟(m30)分析，分别说明各周期结构、"
        "共振与冲突，再给出统一情景排序。每条证据及价位必须标明所属周期，"
        "不同周期的中枢与信号不可混用。\n"
        if freqs == ("day", "m60", "m30") else
        "本次分析级别：" + "、".join(labels[f] for f in freqs) + "。"
        "仅根据本次输入分析，未提供的周期不得推断；只有日线时只说明日线结构与情景排序，"
        "不声称跨周期共振；多周期时分别说明结构、共振与冲突，再给出统一情景排序。"
        "每条证据及价位必须标明所属周期，不同周期的中枢与信号不可混用。\n"
    )
    frames = (data.get("timeframes") or {}).values()
    short_note = ("8. 标有 structure_short 的周期输入不足默认窗口（结构尚未补齐），"
                  "该周期的判断只能作为暂定，不能表述为完整结论。\n"
                  if any(f.get("structure_short") for f in frames) else "")
    return (
        "你是缠论完全分类助手，用贝叶斯方式推理：先有基准判断（先验 prior），"
        "再用证据更新（后验 posterior），置信度只定性不用数字。"
        "只根据给定结构数据推理，不使用外部信息。\n"
        + scope_note +
        "成笔标准只区分严格与宽松；原生点位是形态买卖点，不代表已通过背驰过滤。"
        "status=provisional 是形成中，不能表述为已确认；confirmed 也不是收益保证。"
        "只能解释输入已有的类型、指标及关联结构，不能补造旧锚点或背驰条件。\n"
        "signal_scope=expanded 仅放开笔级中枢数量要求，不表示信号更确定。"
        "context 说明自身或关联一类点的中枢上下文；缺失时不能补造。"
        "strength 仅作信息：value 是力度比而非概率，peak 是 MACD 同向柱峰值，"
        "slope 是价格变化斜率；weaker/equal/stronger 不等同于交易有效性。"
        "unavailable 表示没有可用力度比，不从关联点借用或猜测。\n\n"
        "输出要求：\n"
        "1. 只输出一个 JSON 对象：{\"current_state\": \"...\", \"scenarios\": "
        "[{\"name\", \"prior\", \"evidence_for\", \"evidence_against\", "
        "\"posterior\", \"trigger\", \"boundary\", \"action\", \"basis\", "
        "\"update_watch\"}]}，scenarios 至多 3 个，不要输出任何其他文字。\n"
        "2. current_state 除描述当前状态外，必须含一句「当前后验排序」："
        "哪个情景当前最占优。\n"
        "3. 每个 scenario 按贝叶斯结构推理：\n"
        "   - prior：先验判断，仅凭当前结构状态、不看最新信号时的基准看法，一两句；\n"
        "   - evidence_for / evidence_against：支持 / 削弱该情景的证据列表，"
        "每条必须引用下面数据中出现过的信号 label、价位或中枢上沿/下沿；\n"
        "   - posterior：后验置信度，以「高」「中」「低」三档开头，加一句话理由；\n"
        "   - update_watch：什么新证据出现会改变这个判断，与 trigger/boundary 呼应。\n"
        "4. 禁止输出概率或百分比数字（不写「概率」「%」），置信度只用高/中/低定性表述。\n"
        "5. 每个 scenario 的 trigger 与 boundary 必须引用下面数据中出现过的"
        "具体价位（末bar价、中枢上沿/中枢下沿、依据卡中的信号价/锚定价）。\n"
        "6. basis 必须引用 recent_evidence 中依据卡的内容（信号类型与价位）。\n"
        "7. 输出文本中的日期一律用 MM-DD 格式（需区分年份时用 MM-DD-YY），"
        "不要写 yyyy-mm-dd。\n" + short_note + "\n"
        "数据（JSON）：\n" + body + "\n"
    )


def _normalize_scenario(s: dict) -> dict:
    """贝叶斯字段最小适配：evidence 列表容忍字符串/缺失，旧格式字段缺失不补。"""
    for k in ("evidence_for", "evidence_against"):
        v = s.get(k)
        if isinstance(v, str):
            s[k] = [v]
        elif isinstance(v, list):
            s[k] = [x for x in v if isinstance(x, str)]
        else:
            s.pop(k, None)
    return s


def _parse_llm_output(raw: str) -> dict | None:
    """从 LLM 输出提取 JSON（容忍 ```json 围栏与前后杂文本）；失败返回 None。"""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    scenarios, current = obj.get("scenarios"), obj.get("current_state")
    if not isinstance(scenarios, list) or not isinstance(current, str):
        return None
    scenarios = [_normalize_scenario(s) for s in scenarios
                 if isinstance(s, dict)][:3]
    return {"current_state": current, "scenarios": scenarios}


def _parse_tokens(raw: str | None) -> dict | None:
    """前端回传的 meta.analysis_tokens（URL 编码的 JSON 对象字符串）；缺失或无法解析为 None。"""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


RETIRED_DETAIL = "不再提供 5 分与 15 分周期，请改用 30 分及以上"


def reject_retired_freq(freq: str) -> None:
    """已下线周期明确回 400（不静默改成别的周期冒充）；页面把旧链接的 m5/m15 归到 m30 后再请求。"""
    if freq in api_view_log.RETIRED_FREQS:
        raise HTTPException(status_code=400, detail=RETIRED_DETAIL)


@router.get("/api/analysis")
def api_analysis(code: str = Query(..., min_length=2),
                 freq: str = Query("day", pattern="^(day|m30|m60|m15|m5)$"),
                 rule_profile: Annotated[Literal["strict", "relaxed"], Query()] = "strict",
                 signal_scope: Annotated[Literal["standard", "expanded"], Query()] = "expanded",
                 adjust: Annotated[Literal["qfq", "raw"], Query()] = "qfq",
                 tokens: Annotated[str | None, Query()] = None):
    reject_retired_freq(freq)
    t0 = time.monotonic()
    rules = profile_identity(rule_profile, signal_scope)
    freqs = engine_chart_payload.analysis_periods(code)
    response_identity = {**rules, "adjust": adjust, "tokens": None,
                         "data_version": None, "data_versions": {},
                         "analysis_scope": "multi_timeframe", "freqs": list(freqs)}
    response_identity["analysis_calculation_id"] = engine_chart_payload.analysis_calculation_id(
        rules["calculation_id"], freqs)
    if getattr(engine_data, "is_demo", lambda: False)():
        return {"status": "disabled", "hash": None, "current_state": None,
                "scenarios": [], "cached": False, **response_identity}
    if not engine_llm.is_configured():
        return {"status": "unconfigured", "hash": None,
                "current_state": None, "scenarios": [], "cached": False, **response_identity}

    bundle = engine_chart_payload.read_bundle(code, adjust)
    freqs = tuple(bundle)
    response_identity["freqs"] = list(freqs)
    response_identity["analysis_calculation_id"] = engine_chart_payload.analysis_calculation_id(
        rules["calculation_id"], freqs)
    current = {frame: (bundle.get(frame) or {}).get("token") for frame in freqs}
    expected = _parse_tokens(tokens)
    # 先比令牌：页面据以分析的数据已被定稿、修订或隔离改变（或旧页面没带令牌、组合已变）时一律 409，
    # 即使隔离后某周期已无数据（令牌仍是当前视图身份）；不调用模型，要求整窗重载（spec §6.3）。
    # 组合未变而某周期读不出（读失败或无事实）不是数据更新：回 502，免得页面反复整窗重载
    stale = JSONResponse(status_code=409, content={"detail": "数据已更新", "tokens": current})
    if expected is None or set(expected) != set(freqs):
        return stale
    if any(bundle.get(frame) is None for frame in freqs):
        raise HTTPException(status_code=502, detail="分析数据暂不可用")
    if expected != current:
        return stale
    if any(not (bundle.get(frame) or {}).get("bars") for frame in freqs):
        raise HTTPException(status_code=502, detail="分析数据暂不可用")
    response_identity["tokens"] = current
    # 输入未取够默认窗口的周期：照常分析，但在 prompt 与响应里标出，不冒充完整结论
    short = [frame for frame in freqs if engine_chart_payload.is_structure_short(bundle[frame])]
    response_identity["structure_short"] = short

    prompt_frames = {}
    for frame in freqs:
        dataset = bundle[frame]
        bars = dataset["bars"]
        data_version = engine_compute_cache.dataset_version(dataset)
        response_identity["data_versions"][frame] = data_version
        try:
            hit = engine_compute_cache.get(code, frame, data_version, rules["calculation_id"])
            if hit is not None:
                structure, sig, evidence = hit["structure"], hit["sig"], hit["evidence"]
            else:
                result = chanpy_adapter.compute_analysis(bars, frame, rule_profile=rule_profile, signal_scope=signal_scope)
                structure, sig, evidence = result["structure"], result["sig"], result["evidence"]
                engine_compute_cache.put(code, frame, data_version, structure, sig, evidence,
                                         calculation_id=rules["calculation_id"])
        except Exception as e:
            log.exception("analysis calculation failed code=%s freq=%s", code, frame)
            raise HTTPException(status_code=502, detail="结构计算暂不可用") from e
        prompt_frames[frame] = collect_prompt_data(code, frame, bars, structure, sig, evidence,
                                                   structure_short=frame in short)

    response_identity["data_version"] = data_identity.version([], {
        "scope": ANALYSIS_SCOPE_VERSION, "data_versions": response_identity["data_versions"]})
    prompt = build_prompt({"code": code, "analysis_scope": ANALYSIS_SCOPE_VERSION,
                           "rule_profile": response_identity["rule_profile"],
                           "calculation_id": response_identity["calculation_id"],
                           "signal_scope": response_identity["signal_scope"],
                           "timeframes": prompt_frames})
    # 结果缓存键 = prompt 文本哈希：miss ⇔ LLM 输入真的变化，
    # prompt 之外的 bar/信号噪声不再造成假 miss
    h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    with _analysis_lock(h):
        return _analyze_combined(code, h, prompt, prompt_frames, response_identity, t0)


def _analyze_combined(code: str, h: str, prompt: str, prompt_frames: dict,
                      response_identity: dict, t0: float) -> dict:
    cache_file = _cache_dir() / f"{h}.json"
    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached.get("status") == "llm_error":
            # 失败条目：TTL 内直接 502 不重试；过期则落到正常 LLM 调用流程
            if time.time() - cached.get("fetched_at", 0) < FAILURE_CACHE_TTL:
                raise HTTPException(status_code=502,
                                    detail=cached.get("error", "LLM 调用失败"))
        else:
            cached["cached"] = True
            cached.update(response_identity)
            log.info("[timing] analysis code=%s freq=%s cache=hit llm=0ms total=%dms",
                     code, ANALYSIS_SCOPE_VERSION, int((time.monotonic() - t0) * 1000))
            return cached

    try:
        t_llm = time.monotonic()
        raw = engine_llm.analyze(prompt)
    except engine_llm.LLMError as e:
        # 失败也落短 TTL 缓存：故障期内后续请求直接 502，不再重复调 LLM
        cache_dir = _cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(
            {"status": "llm_error", "error": str(e), "hash": h,
             "fetched_at": time.time()}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        raise HTTPException(status_code=502, detail=str(e)) from e

    parsed = _parse_llm_output(raw)
    if parsed is None:  # JSON 解析失败：降级返回 raw 文本
        parsed = {"current_state": None, "scenarios": [], "raw": raw}

    result = {"status": "ok", "hash": h, "cached": False,
              "ref_bar_dt": prompt_frames["day"]["last_bar"]["dt"], **parsed, **response_identity}
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    log.info("[timing] analysis code=%s freq=%s cache=miss llm=%dms total=%dms",
             code, ANALYSIS_SCOPE_VERSION, int((time.monotonic() - t_llm) * 1000),
             int((time.monotonic() - t0) * 1000))
    return result
