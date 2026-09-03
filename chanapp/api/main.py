"""缠论分析自用 web app API。

GET /api/chart?code=sh000001&freq=day|m30|m60
返回 {kline, macd{rows, beichi_links}, structure{bi,xd,zs,forming}, signals,
      forming_signal, evidence, channels, resonance, meta{source, fqf,
      fetch_time, degraded, degraded_note, from_cache, bars}}
resonance：多级别共振角标（日/60/30 三级各取最新确认信号、最新中枢、雏形信号），
纯计算零新数据，复用 compute_cache；某级取数/计算失败则该级缺省，不影响主响应。

自选股：GET/POST /api/watchlist，DELETE /api/watchlist/{code}，
POST /api/watchlist/{code}/star（星标置顶；文件保持 append 序，响应星标在前）
持久化到 WATCHLIST_PATH（env，默认 chanapp/watchlist.json）。

联想搜索：GET /api/search?q=（外部联想接口，返回 [{code,name,type}]，仅 sh/sz/hk；
适配层未安装时 503）。

行情显示层：GET /api/quotes（自选股快照）、GET /api/f10?code=（F10+资金流+板块涨幅）；
适配层未安装时 503。

AI 完全分类：GET /api/analysis?code=&freq=（chanapp/api/analysis.py，
结构哈希缓存到 chanapp/.cache/analysis/，LLM 未配置时返回 unconfigured）。

运行：.venv-chan/bin/python -m uvicorn chanapp.api.main:app --host 127.0.0.1 --port 8899
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 允许以模块（python -m uvicorn chanapp.api.main:app）或脚本方式运行
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT.parent))

from chanapp.api import analysis as api_analysis  # noqa: E402
from chanapp.engine import channels as engine_channels  # noqa: E402
from chanapp.engine import compute_cache as engine_compute_cache  # noqa: E402
from chanapp.engine import data as engine_data  # noqa: E402
from chanapp.engine import evidence as engine_evidence  # noqa: E402
from chanapp.engine import signals as engine_signals  # noqa: E402
from chanapp.engine import structure as engine_structure  # noqa: E402
from chanapp.engine import warmer  # noqa: E402

log = logging.getLogger(__name__)

# chanapp.* 的 INFO 日志（[timing] 等）在 uvicorn 默认配置下会被 root 吞掉，显式配置
_chanapp_log = logging.getLogger("chanapp")
if not _chanapp_log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _chanapp_log.addHandler(_h)
_chanapp_log.setLevel(logging.INFO)
_chanapp_log.propagate = False  # root 日后若也配 handler，避免日志双写

# 行情显示层 / 联想搜索为可选数据适配层（私有数据源模块）：
# 未安装时对应路由返回 503，K 线/结构/信号主链路不受影响。
try:  # noqa: E402
    from chanapp.engine import display_feed as engine_feed
except ImportError:  # 适配层未安装
    engine_feed = None
try:  # noqa: E402
    from chanapp.engine import search as engine_search
except ImportError:  # 适配层未安装
    engine_search = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    warmer.start()
    yield


app = FastAPI(title="chanapp", docs_url=None, redoc_url=None, lifespan=lifespan)


# ---------- 多级别共振角标 ----------


_RESONANCE_FREQS = ("day", "m60", "m30")


def _level_summary(code: str, freq: str) -> dict | None:
    """单级别摘要：最新确认信号 / 最新中枢（含末收是否在中枢内）/ 雏形信号。

    优先复用 compute_cache（末 bar dt 一致才命中），否则现算并回填；
    取数或计算失败返回 None（该级角标缺省）。
    """
    try:
        dataset = engine_data.get_bars(code, freq)
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


def _resonance(code: str) -> list[dict]:
    """三级别并行摘要（pool.map 保序）；单级失败返回 None → 该级缺省，不拖垮主响应。"""
    with ThreadPoolExecutor(max_workers=len(_RESONANCE_FREQS),
                            thread_name_prefix="resonance") as pool:
        summaries = list(pool.map(lambda f: _level_summary(code, f), _RESONANCE_FREQS))
    return [s for s in summaries if s is not None]


@app.get("/api/chart")
def api_chart(code: str = Query(..., min_length=2),
              freq: str = Query("day", pattern="^(day|m30|m60)$")):
    t0 = time.monotonic()
    try:
        dataset = engine_data.get_bars(code, freq)
    except Exception as e:  # 数据层错误统一成 502
        raise HTTPException(status_code=502, detail=str(e)) from e
    t_bars = time.monotonic()

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

    resonance = _resonance(code)
    t_res = time.monotonic()
    log.info("[timing] chart code=%s freq=%s bars=%dms compute=%dms resonance=%dms total=%dms",
             code, freq, int((t_bars - t0) * 1000), int((t_compute - t_bars) * 1000),
             int((t_res - t_compute) * 1000), int((t_res - t0) * 1000))
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


# ---------- 联想搜索（自选股添加） ----------


@app.get("/api/search")
def api_search(q: str = Query("")) -> list[dict]:
    """外部联想接口：返回 [{code, name, type}]，仅 sh/sz/hk。空 q 返回 []。"""
    if engine_search is None:
        raise HTTPException(status_code=503, detail="搜索适配层未安装")
    try:
        return engine_search.search(q)
    except Exception as e:  # 上游失败统一成 502
        raise HTTPException(status_code=502, detail=f"联想搜索失败：{e}") from e


# ---------- 自选股 CRUD ----------

_WATCHLIST_SEED = _PKG_ROOT / "watchlist.json"


def _watchlist_path() -> Path:
    # 请求时解析，测试可通过 WATCHLIST_PATH 注入临时路径
    return Path(os.environ.get("WATCHLIST_PATH") or _WATCHLIST_SEED)


def _read_watchlist_raw() -> list[dict]:
    # 文件保持 append 序；旧数据无 starred 字段，归一化为 False
    p = _watchlist_path()
    if not p.exists() and p != _WATCHLIST_SEED:
        p = _WATCHLIST_SEED
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    return [dict(w, starred=bool(w.get("starred"))) for w in raw]


def _ordered(items: list[dict]) -> list[dict]:
    # 星标置顶（稳定排序，组内保持 append 序）；排序只作用于响应，不落盘
    return sorted(items, key=lambda w: 0 if w["starred"] else 1)


def _save_watchlist(items: list[dict]) -> None:
    _watchlist_path().write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class WatchItem(BaseModel):
    code: str
    name: str


@app.get("/api/watchlist")
def list_watchlist() -> list[dict]:
    return _ordered(_read_watchlist_raw())


@app.post("/api/watchlist")
def add_watch(item: WatchItem) -> list[dict]:
    items = _read_watchlist_raw()
    if any(w["code"] == item.code for w in items):
        raise HTTPException(status_code=409, detail=f"{item.code} 已在自选中")
    items.append({"code": item.code, "name": item.name, "starred": False})
    _save_watchlist(items)
    return _ordered(items)


@app.delete("/api/watchlist/{code}")
def remove_watch(code: str) -> list[dict]:
    items = _read_watchlist_raw()
    kept = [w for w in items if w["code"] != code]
    if len(kept) == len(items):
        raise HTTPException(status_code=404, detail=f"{code} 不在自选中")
    _save_watchlist(kept)
    return _ordered(kept)


@app.post("/api/watchlist/{code}/star")
def toggle_star(code: str) -> list[dict]:
    items = _read_watchlist_raw()
    hit = next((w for w in items if w["code"] == code), None)
    if hit is None:
        raise HTTPException(status_code=404, detail=f"{code} 不在自选中")
    hit["starred"] = not hit["starred"]
    _save_watchlist(items)
    return _ordered(items)


# ---------- 行情显示层（快照 / F10，适配层可选） ----------


@app.get("/api/quotes")
def api_quotes():
    if engine_feed is None:
        raise HTTPException(status_code=503, detail="行情快照适配层未安装")
    items = _read_watchlist_raw()
    try:
        r = engine_feed.get_quotes([it["code"] for it in items])
    except Exception as e:  # 数据层错误统一成 502
        raise HTTPException(status_code=502, detail=f"快照失败：{e}") from e
    return {"quotes": r["quotes"], "degraded": r["degraded"],
            "fetch_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))}


@app.get("/api/f10")
def api_f10(code: str = Query(..., pattern="^(sh|sz|hk)\\d+$")):
    if engine_feed is None:
        raise HTTPException(status_code=503, detail="F10 适配层未安装")
    try:
        r = engine_feed.get_f10(code)
    except Exception as e:  # 数据层错误统一成 502
        raise HTTPException(status_code=502, detail=f"F10 失败：{e}") from e
    return {"f10": r["f10"], "flow": r["flow"], "industry_pct": r["industry_pct"],
            "degraded": r["degraded"],
            "fetch_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))}


# ---------- AI 完全分类 ----------
app.include_router(api_analysis.router)


# 静态托管 web/（含 index.html）
_WEB_DIR = _PKG_ROOT / "web"


@app.get("/")
def index():
    return FileResponse(_WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=_WEB_DIR), name="web")
