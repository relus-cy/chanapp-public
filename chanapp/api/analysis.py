"""AI 完全分类端点：GET /api/analysis?code=&freq=

组装与 /api/chart 同源的结构数据（bars/structure/signals/evidence），
构造完全分类 prompt 调 LLM，按结构哈希缓存结果：
- 缓存文件：chanapp/.cache/analysis/{sha256(code+freq+末bar dt+最新信号 dt)}.json，
  目录可用 ANALYSIS_CACHE_DIR 环境变量注入（测试用，同 WATCHLIST_PATH 模式）。
- llm.is_configured() 为 False → 直接返回 status "unconfigured"，不取数不调 LLM。
- LLM 输出解析不成 JSON → 降级返回 raw 文本（status 仍 "ok"，scenarios 为空）。

Prompt 契约（build_prompt）：只喂 {code, freq, 末bar dt/价, 最近中枢上沿/下沿,
最近 5 个信号依据卡, forming 信号}；要求输出 JSON {"current_state", "scenarios"
[≤3, 各含 name/prior/evidence_for/evidence_against/posterior/trigger/boundary/
action/basis/update_watch]}，按贝叶斯结构推理：prior 先验、evidence_for/against
证据更新、posterior 后验置信（定性三档 高/中/低）、update_watch 更新观察点；
current_state 含一句「当前后验排序」。禁止输出概率数字；证据与 trigger/boundary
必须引用输入中出现过的信号 label 与价位（约束写进 prompt 文本，不做输出校验）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from chanapp.engine import compute_cache as engine_compute_cache
from chanapp.engine import data as engine_data
from chanapp.engine import evidence as engine_evidence
from chanapp.engine import llm as engine_llm
from chanapp.engine import signals as engine_signals
from chanapp.engine import structure as engine_structure

_PKG_ROOT = Path(__file__).resolve().parent.parent

# LLM 失败条目（status="llm_error"）短 TTL：故障期内抑制重试，避免
# 每分钟自动刷新都重调 LLM + 30s 超时挂起；过期后自动恢复重试。
FAILURE_CACHE_TTL = 600

log = logging.getLogger(__name__)

router = APIRouter()


def _cache_dir() -> Path:
    # 请求时解析，测试可通过 ANALYSIS_CACHE_DIR 注入临时目录
    return Path(os.environ.get("ANALYSIS_CACHE_DIR")
                or _PKG_ROOT / ".cache" / "analysis")


def _strip_pct(text: str) -> str:
    """依据卡文案去掉「衰竭度 N%」百分数（百分比数字不喂给 LLM）。"""
    return re.sub(r"，衰竭度\s*[\d.]+%", "", text)


def collect_prompt_data(code: str, freq: str, bars: list[dict],
                        structure: dict, sig: dict,
                        evidence_cards: list[dict]) -> dict:
    """从组装数据提取 prompt 输入：末bar、最近中枢、最近 5 张依据卡、forming 信号。"""
    last = bars[-1]
    zss = structure.get("zs") or []
    zs = zss[-1] if zss else None
    cards = sorted(evidence_cards, key=lambda c: c["dt"], reverse=True)[:5]
    return {
        "code": code,
        "freq": freq,
        "last_bar": {"dt": last["dt"], "close": last["close"]},
        "latest_zs": ({"dt0": zs["dt0"], "dt1": zs["dt1"],
                       "中枢上沿": zs["zg"], "中枢下沿": zs["zd"]} if zs else None),
        "recent_evidence": [
            {"dt": c["dt"], "type": c["type"], "price": c["price"],
             "side": c["side"], "text": _strip_pct(c["text"])}
            for c in cards
        ],
        "forming_signal": sig.get("forming_signal"),
    }


def build_prompt(data: dict) -> str:
    """完全分类 prompt：数据 JSON + 贝叶斯输出契约（先验/证据/后验，禁概率数字）。"""
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return (
        "你是缠论完全分类助手，用贝叶斯方式推理：先有基准判断（先验 prior），"
        "再用证据更新（后验 posterior），置信度只定性不用数字。"
        "只根据给定结构数据推理，不使用外部信息。\n\n"
        "数据（JSON）：\n" + body + "\n\n"
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
        "每条必须引用上面数据中出现过的信号 label、价位或中枢上沿/下沿；\n"
        "   - posterior：后验置信度，以「高」「中」「低」三档开头，加一句话理由；\n"
        "   - update_watch：什么新证据出现会改变这个判断，与 trigger/boundary 呼应。\n"
        "4. 禁止输出概率或百分比数字（不写「概率」「%」），置信度只用高/中/低定性表述。\n"
        "5. 每个 scenario 的 trigger 与 boundary 必须引用上面数据中出现过的"
        "具体价位（末bar价、中枢上沿/中枢下沿、依据卡中的信号价/锚定价）。\n"
        "6. basis 必须引用 recent_evidence 中依据卡的内容（信号类型与价位）。\n"
        "7. 输出文本中的日期一律用 MM-DD 格式（需区分年份时用 MM-DD-YY），"
        "不要写 yyyy-mm-dd。\n"
    )


def _structure_hash(code: str, freq: str, bars: list[dict], sig: dict) -> str:
    """缓存键：sha256(code+freq+末bar dt+最新信号 dt)。"""
    dts = [s["dt"] for s in sig.get("signals", [])]
    forming = sig.get("forming_signal")
    if forming:
        dts.append(forming["dt"])
    latest_signal = max(dts) if dts else ""
    raw = "|".join([code, freq, bars[-1]["dt"], latest_signal])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


@router.get("/api/analysis")
def api_analysis(code: str = Query(..., min_length=2),
                 freq: str = Query("day", pattern="^(day|m30|m60|m15|m5)$")):
    t0 = time.monotonic()
    if not engine_llm.is_configured():
        return {"status": "unconfigured", "hash": None,
                "current_state": None, "scenarios": [], "cached": False}

    try:
        dataset = engine_data.get_bars(code, freq)
    except Exception as e:  # 数据层错误统一成 502
        raise HTTPException(status_code=502, detail=str(e)) from e

    bars = dataset["bars"]
    hit = engine_compute_cache.get(code, freq, bars[-1]["dt"])
    if hit is not None:
        structure, sig, evidence = hit["structure"], hit["sig"], hit["evidence"]
    else:
        structure = engine_structure.compute_structure(bars, code, freq)
        sig = engine_signals.compute_signals(bars, structure)
        evidence = engine_evidence.build_evidence(sig["signals"], structure)
        engine_compute_cache.put(code, freq, bars[-1]["dt"], structure, sig, evidence)

    h = _structure_hash(code, freq, bars, sig)
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
            log.info("[timing] analysis code=%s freq=%s cache=hit llm=0ms total=%dms",
                     code, freq, int((time.monotonic() - t0) * 1000))
            return cached

    prompt = build_prompt(collect_prompt_data(code, freq, bars, structure,
                                              sig, evidence))
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
              "ref_bar_dt": bars[-1]["dt"], **parsed}
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    log.info("[timing] analysis code=%s freq=%s cache=miss llm=%dms total=%dms",
             code, freq, int((time.monotonic() - t_llm) * 1000),
             int((time.monotonic() - t0) * 1000))
    return result
