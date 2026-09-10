"""缠论分析自用 web app API。

GET /api/chart?code=sh000001&freq=day|m30|m60&rule_profile=strict|relaxed&signal_scope=standard|expanded
返回 {kline, macd{rows}, structure{bi,xd,zs,zs_xd,forming}, signals,
      rule_profile, calculation_id, evidence, channels, resonance, meta{source, fqf,
      fetch_time, degraded, degraded_note, from_cache, stale, stale_age_s, bars}}
meta.stale：K 线缓存已过 TTL 但本次回的是旧数据（stale-while-revalidate，后台异步
刷新中），stale_age_s 为距缓存写入的秒数；新鲜数据 stale=False 且无 stale_age_s。
resonance：多周期摘要（日/60/30 分别列出最新笔/段点位、状态及中枢），
纯计算零新数据，复用 compute_cache；某级取数/计算失败则该级缺省，不影响主响应。

自选股：GET/POST /api/watchlist，DELETE /api/watchlist/{code}，
POST /api/watchlist/{code}/star（星标置顶；文件保持 append 序，响应星标在前），
PUT /api/watchlist/{code}/tags（自定义标签：strip/去空/保序去重/每条≤12 字符/≤8 条）
持久化到 WATCHLIST_PATH（env，默认 chanapp/watchlist.json）。

联想搜索：GET /api/search?q=（外部联想接口，返回 [{code,name,type}]，仅 sh/sz/hk；
适配层未安装时 503）。

行情显示层：GET /api/quotes（自选股快照）、GET /api/f10?code=（F10+资金流+板块涨幅）；
适配层未安装时 503。v1.3.1 起 SWR：缓存过期先回旧数据并后台异步刷新，仅首冷同步抓取；
degraded=True 一律表示「本次回的是过期旧缓存」，此时 fetch_time 为旧缓存时间（数据年龄），
新鲜数据 degraded=False。

AI 完全分类：GET /api/analysis?code=&freq=（chanapp/api/analysis.py，
结构哈希缓存到 chanapp/.cache/analysis/，LLM 未配置时返回 unconfigured）。

运行：.venv-chan/bin/python -m uvicorn chanapp.api.main:app --host 127.0.0.1 --port 8899
"""
from __future__ import annotations

from typing import Annotated, Literal

import json
import logging
import os
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 允许以模块（python -m uvicorn chanapp.api.main:app）或脚本方式运行
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT.parent))

from chanapp.api import analysis as api_analysis  # noqa: E402
from chanapp.engine import chart_payload as engine_chart_payload  # noqa: E402
from chanapp.engine import data as engine_data  # noqa: E402
from chanapp.engine import warmer  # noqa: E402
from chanapp.engine import supply as supply_state
from chanapp.api import supply as supply_api

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
    owned = supply_api.candidate is not None
    try:
        if owned:
            try:
                supply_state.start_runtime()
            except supply_state.StateUnavailable as exc:
                if exc.reason == "runtime_already_running":
                    raise
                log.error("Supply recovery required: %s", exc.reason)
            else:
                warmer.start()
        yield
    finally:
        if owned:
            supply_state.stop_runtime()


app = FastAPI(title="chanapp", version="1.6.5", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def bind_supply(request, call_next):
    # Static resources and the diagnostic endpoint must survive state damage.
    if not request.url.path.startswith("/api/") or request.url.path == "/api/supply":
        return await call_next(request)
    try:
        captured = supply_state.snapshot()
    except supply_state.StateUnavailable:
        return JSONResponse(status_code=503, content={
            "error_code": "state_unavailable", "recovery_required": True})
    with supply_state.use(captured):
        response = await call_next(request)
        response.headers["X-Supply-Scheme"] = captured.scheme
        response.headers["X-Supply-Generation"] = str(captured.generation)
        response.headers["X-Supply-Epoch"] = captured.epoch
        return response


app.include_router(supply_api.router)


# ---------- /api/chart（组装逻辑在 engine/chart_payload.py，Pages 推送平面共用） ----------


@app.get("/api/chart")
def api_chart(code: str = Query(..., min_length=2),
              freq: str = Query("day", pattern="^(day|m30|m60|m15|m5)$"),
              rule_profile: Annotated[Literal["strict", "relaxed"], Query()] = "strict",
              signal_scope: Annotated[Literal["standard", "expanded"], Query()] = "expanded"):
    t0 = time.monotonic()
    try:
        dataset = engine_data.get_bars(code, freq)
    except Exception as e:  # 数据层错误统一成 502
        raise HTTPException(status_code=502, detail="行情暂不可用") from e
    t_bars = time.monotonic()

    timings: dict = {}
    try:
        payload = engine_chart_payload.build_chart_payload(code, freq, dataset,
                                                           timings=timings, rule_profile=rule_profile, signal_scope=signal_scope)
    except Exception as e:
        log.exception("chart calculation failed code=%s freq=%s profile=%s", code, freq, rule_profile)
        raise HTTPException(status_code=502, detail="结构计算暂不可用") from e
    t_end = time.monotonic()
    log.info("[timing] chart code=%s freq=%s bars=%dms compute=%dms resonance=%dms total=%dms",
             code, freq, int((t_bars - t0) * 1000), timings["compute_ms"],
             timings["resonance_ms"], int((t_end - t0) * 1000))
    return payload


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
_WATCHLIST_LOCK = threading.RLock()


def _watchlist_path() -> Path:
    # 请求时解析，测试可通过 WATCHLIST_PATH 注入临时路径
    return Path(os.environ.get("WATCHLIST_PATH") or _WATCHLIST_SEED)


def _read_watchlist_raw() -> list[dict]:
    # 文件保持 append 序；旧数据无 starred/tags 字段，归一化为 False / []
    with _WATCHLIST_LOCK:
        p = _watchlist_path()
        if not p.exists() and p != _WATCHLIST_SEED:
            p = _WATCHLIST_SEED
        if not p.exists():
            return []
        raw = json.loads(p.read_text(encoding="utf-8"))
        return [dict(w, starred=bool(w.get("starred")), tags=_norm_tags(w.get("tags"))) for w in raw]


def _norm_tags(v) -> list[str]:
    # 仅接受 list，元素只保留 strip 后非空的 str
    if not isinstance(v, list):
        return []
    return [t.strip() for t in v if isinstance(t, str) and t.strip()]


def _ordered(items: list[dict]) -> list[dict]:
    # 星标置顶（稳定排序，组内保持 append 序）；排序只作用于响应，不落盘
    return sorted(items, key=lambda w: 0 if w["starred"] else 1)


def _save_watchlist(items: list[dict]) -> None:
    with _WATCHLIST_LOCK:
        path = _watchlist_path()
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class WatchItem(BaseModel):
    code: str
    name: str


@app.get("/api/watchlist")
def list_watchlist() -> list[dict]:
    with _WATCHLIST_LOCK:
        return _ordered(_read_watchlist_raw())


@app.post("/api/watchlist")
def add_watch(item: WatchItem) -> list[dict]:
    with _WATCHLIST_LOCK:
        items = _read_watchlist_raw()
        if any(w["code"] == item.code for w in items):
            raise HTTPException(status_code=409, detail=f"{item.code} 已在自选中")
        items.append({"code": item.code, "name": item.name, "starred": False, "tags": []})
        _save_watchlist(items)
        return _ordered(items)


@app.delete("/api/watchlist/{code}")
def remove_watch(code: str) -> list[dict]:
    with _WATCHLIST_LOCK:
        items = _read_watchlist_raw()
        kept = [w for w in items if w["code"] != code]
        if len(kept) == len(items):
            raise HTTPException(status_code=404, detail=f"{code} 不在自选中")
        _save_watchlist(kept)
        return _ordered(kept)


@app.post("/api/watchlist/{code}/star")
def toggle_star(code: str) -> list[dict]:
    with _WATCHLIST_LOCK:
        items = _read_watchlist_raw()
        hit = next((w for w in items if w["code"] == code), None)
        if hit is None:
            raise HTTPException(status_code=404, detail=f"{code} 不在自选中")
        hit["starred"] = not hit["starred"]
        _save_watchlist(items)
        return _ordered(items)


class WatchTags(BaseModel):
    tags: list[str]


@app.put("/api/watchlist/{code}/tags")
def set_tags(code: str, body: WatchTags) -> list[dict]:
    with _WATCHLIST_LOCK:
        items = _read_watchlist_raw()
        hit = next((w for w in items if w["code"] == code), None)
        if hit is None:
            raise HTTPException(status_code=404, detail=f"{code} 不在自选中")
        tags: list[str] = []
        for raw in body.tags:
            t = raw.strip()[:12]
            if t and t not in tags:
                tags.append(t)
        hit["tags"] = tags[:8]
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
        raise HTTPException(status_code=502, detail="快照失败，暂不可用") from e
    return {"scheme": supply_state.current().scheme, "generation": supply_state.current().generation,
            "epoch": supply_state.current().epoch,
            "missing_codes": r.get("missing_codes", []), "invalid_codes": r.get("invalid_codes", []),
            "data_version": r.get("data_version"), "meta": r.get("meta", {}), "quotes": r["quotes"], "degraded": r["degraded"],
            "fetch_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))}


@app.get("/api/f10")
def api_f10(code: str = Query(..., pattern="^(sh|sz|hk)\\d+$")):
    if engine_feed is None:
        raise HTTPException(status_code=503, detail="F10 适配层未安装")
    try:
        r = engine_feed.get_f10(code)
    except Exception as e:  # 数据层错误统一成 502
        raise HTTPException(status_code=502, detail="基本资料暂不可用") from e
    return {"scheme": supply_state.current().scheme, "generation": supply_state.current().generation,
            "epoch": supply_state.current().epoch,
            "data_version": r.get("data_version"), "meta": r.get("meta", {}),
            "f10": r["f10"], "flow": r["flow"], "industry_pct": r["industry_pct"],
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
