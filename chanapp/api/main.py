"""缠论分析自用 web app API。

GET /api/chart?code=&freq=week|day|m60|m30&adjust=qfq|raw&rule_profile=&signal_scope=
（5 分、15 分已下线：freq=m5|m15 回 400「不再提供」，不读数据）
返回 {kline, macd{rows}, structure{bi,xd,zs,zs_xd,forming}, signals, rule_profile, calculation_id,
      evidence, channels, resonance, meta}；meta 为门面返回体（去掉 bars）加 data_version、bars 根数、
first_dt、last_dt 与 analysis_tokens {day, m60, m30}（AI 请求令牌的唯一来源）。
响应带弱 ETag（整包哈希，剔除按秒变化的 stale_age_s）与 Cache-Control: no-cache；If-None-Match 命中回 304 空体。
带 before 为历史分页：只支持 day/week/m60/m30；必须回传 meta.token，不符或缺失返回
409 {"detail": "数据已更新", "token": 当前令牌}（扁平响应体），客户端整窗重载。
主图当前周期与 resonance、analysis_tokens 出自同一次 bundle 读取（当前周期不在日/60/30 时一并读入）。
resonance：多周期摘要（日/60/30）；某级缺数据则该级缺省；structure_short 为真表示该级输入未取够默认窗口。

GET /api/status：采集器状态（自选股的数据集新鲜度、缺口、绑定与探针、额度）。

GET /api/session：各市场此刻是否在交易时段，{checked_at, markets:{cn:{open}, hk:{open}}}；
按东八区现在时刻、交易日历钩子与 engine/session.py 时段计算，Cache-Control: no-store。
前端自动刷新与「交易中 / 已收盘」只认这个结果。

自选股：GET/POST /api/watchlist，DELETE /api/watchlist/{code}，
POST /api/watchlist/{code}/star（星标置顶；文件保持 append 序，响应星标在前），
PUT /api/watchlist/{code}/tags（自定义标签：strip/去空/保序去重/每条≤12 字符/≤8 条）
持久化到个人自选路径（WATCHLIST_PATH，否则由实例目录或缓存根派生，见 engine/instance_paths.py）；
仓库 watchlist.json 只作种子：个人文件不存在时读它，写入不回写种子。

搜索查看记录：POST /api/views {code, name, freq, adjust}（用户打开图表时记一次事件并更新最近列表），
GET /api/views → {recent: [{code, name, freq, adjust, first_viewed_at, last_viewed_at, views, watched}]}（最近在前）；
持久化到 VIEW_LOG_PATH；未设时设了 WATCHLIST_PATH 则与它同目录，否则在实例目录，再否则在缓存目录
（engine/instance_paths.py）。
/api/chart?refetch=1 先整段重取主图周期与共振依赖的分析窗口再读取，结果在响应头 X-Refetch-Status
（ok / partial / failed / busy / disabled；busy 为同一标的已有重拉或追赶在途、本次没有重取；
demo 模式为 disabled，照常读取）。同一标的重拉在途时读取只读现有快照（不等它的锁），
没有可服务快照时 503「正在更新」。

联想搜索：GET /api/search?q=（外部联想接口，返回 [{code,name,type}]，仅 sh/sz/hk；
适配层未安装时 503）。

行情显示层：GET /api/quotes（自选股快照）、GET /api/quote?code=（单代码报价：搜索查看的非自选代码；A 股同自选行
取事实派生价格，港股走显示层单代码缓存、不覆盖自选缓存）、GET /api/f10?code=（F10+资金流+板块涨幅）；
适配层未安装时 503。/api/quotes 的 A 股行只取门面 quote（K 线事实派生）的 price、pct、limit_up、
price_time、price_label（最新/昨收），事实缺失时价格为空并标 price_unavailable；港股行保持显示层值。缓存过期先回旧数据并后台异步刷新，仅首冷同步抓取；
degraded=True 一律表示「本次回的是过期旧缓存」，此时 fetch_time 为旧缓存时间（数据年龄）。

AI 完全分类：GET /api/analysis?code=&freq=&adjust=&tokens=（chanapp/api/analysis.py）。

运行：.venv-chan/bin/python -m uvicorn chanapp.api.main:app --host 127.0.0.1 --port 8899
"""
from __future__ import annotations

from typing import Annotated, Literal

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# 允许以模块（python -m uvicorn chanapp.api.main:app）或脚本方式运行
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT.parent))

import chanapp  # noqa: E402
from chanapp.api import analysis as api_analysis  # noqa: E402
from chanapp.api import view_log as api_view_log  # noqa: E402
from chanapp.engine import chart_payload as engine_chart_payload  # noqa: E402
from chanapp.engine import data as engine_data  # noqa: E402
from chanapp.engine import data_identity  # noqa: E402
from chanapp.engine import instance_paths, period_preferences  # noqa: E402
from chanapp.engine import session as engine_session  # noqa: E402

log = logging.getLogger(__name__)

# chanapp.* 的 INFO 日志（[timing] 等）在 uvicorn 默认配置下会被 root 吞掉，显式配置
_chanapp_log = logging.getLogger("chanapp")
if not _chanapp_log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _chanapp_log.addHandler(_h)
_chanapp_log.setLevel(logging.INFO)
_chanapp_log.propagate = False  # root 日后若也配 handler，避免日志双写

# 行情显示层 / 联想搜索为可选数据适配层：
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
    # 配置失败必须阻止启动，不能被下方采集器的运行故障隔离吞掉。
    with engine_data.configure_instance():
        async with collector_lifespan():
            yield


@asynccontextmanager
async def collector_lifespan():
    started = False
    try:
        started = engine_data.start_collector(lambda: [w["code"] for w in _read_watchlist_raw()])
    except Exception:
        log.error("采集器启动失败", exc_info=True)
    try:
        yield
    finally:
        if started:
            engine_data.stop_collector()


app = FastAPI(title="chanapp", version=chanapp.__version__, docs_url=None, redoc_url=None, lifespan=lifespan)


class PeriodSelection(BaseModel):
    selected: list[str]
    revision: str


def _period_preferences():
    preferences = period_preferences.current()
    if preferences is None:
        raise HTTPException(status_code=503, detail="周期偏好尚未初始化")
    return preferences


@app.get("/api/periods")
def api_periods():
    return JSONResponse(_period_preferences().snapshot(), headers={"Cache-Control": "no-store"})


@app.put("/api/periods")
def api_save_periods(selection: PeriodSelection):
    try:
        state = _period_preferences().save(selection.selected, selection.revision)
    except period_preferences.Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="周期偏好未能保存，请重试") from exc
    return JSONResponse(state, headers={"Cache-Control": "no-store"})


# ---------- /api/chart（组装逻辑在 engine/chart_payload.py，Pages 推送平面共用） ----------

_BEFORE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ ]\d{2}:\d{2}(:\d{2})?)?$")


def _normalize_before(before: str) -> str:
    """before 游标归一：ISO 'T' 转库内空格格式（字符串比较口径一致）；
    非法形态明确 400 降级，不静默按无参返回最新整窗。"""
    normalized = before.replace("T", " ")
    if not _BEFORE_RE.match(normalized):
        raise HTTPException(status_code=400,
                            detail="before 格式无效（YYYY-MM-DD[ HH:mm[:ss]]）")
    return normalized


HISTORY_PERIODS = ("day", "week", "m60", "m30")


def _conflict(**body) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": "数据已更新", **body})


def _without_age(value):
    if isinstance(value, dict):
        return {k: _without_age(v) for k, v in value.items() if k != "stale_age_s"}
    if isinstance(value, list):
        return [_without_age(v) for v in value]
    return value


def _etag_matches(header, etag) -> bool:
    """If-None-Match 的弱比较（RFC 9110 §13.1.2）：* 或列表中任一实体标签去掉 W/ 后与当前标签相同即命中。"""
    if not header:
        return False
    if header.strip() == "*":
        return True
    opaque = etag.removeprefix("W/")
    return any(t.strip().removeprefix("W/") == opaque for t in header.split(","))


def _chart_etag(payload) -> str:
    """弱 ETag：整包哈希，只剔除 stale_age_s（stale 时按秒变化，数据与状态都没变；否则 60 秒轮询每次整包重传）。
    stale 翻转、新提交与其余字段变化照常改变 ETag。按语义等价比较，所以用弱校验器（RFC 9110 §8.8.1）。"""
    canon = json.dumps(_without_age(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False)
    return 'W/"' + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32] + '"'


@app.get("/api/chart")
def api_chart(request: Request,
              code: str = Query(..., min_length=2),
              freq: str = Query("day", pattern="^(week|day|m60|m30|m15|m5)$"),
              adjust: Annotated[Literal["qfq", "raw"], Query()] = "qfq",
              rule_profile: Annotated[Literal["strict", "relaxed"], Query()] = "strict",
              signal_scope: Annotated[Literal["standard", "expanded"], Query()] = "expanded",
              before: str | None = Query(None),
              limit: int = Query(520, ge=1, le=2000),
              token: str | None = Query(None),
              refetch: bool = Query(False)):
    api_analysis.reject_retired_freq(freq)
    prefs = period_preferences.current()
    if prefs is not None and not prefs.allows(code, freq):
        raise HTTPException(status_code=400, detail="该周期未勾选或当前市场无法合成")
    if before is not None:
        return _chart_history(request, code, freq, _normalize_before(before), limit, adjust, token)

    t0 = time.monotonic()
    extra = {}
    if refetch:
        # 手动重拉：先整段重取主图周期与共振依赖的分析窗口（不改变关注状态），再照常读取；失败保留旧数据，照常服务。
        # 结果（ok / partial / failed / busy / disabled）放在响应头 X-Refetch-Status，不进响应体与 ETag
        try:
            status = (engine_data.refetch_window(code, freq) or {}).get("status") or "failed"
        except Exception:
            log.warning("手动重拉失败 code=%s", code, exc_info=True)
            status = "failed"
        extra["X-Refetch-Status"] = status
    try:
        # 主图与共振、AI 令牌一次读取（spec §6.3）：不再先单读主图、后读共振
        dataset, bundle = engine_chart_payload.read_chart_inputs(code, freq, adjust)
    except Exception as e:  # 数据层错误统一成 502；同一代码重拉在途且没有可服务快照时 503（不等重拉的锁）
        if isinstance(e, engine_data.RefetchBusy):
            raise HTTPException(status_code=503, detail="正在更新这只标的，请稍后再试", headers=extra or None) from e
        raise HTTPException(status_code=502, detail="行情暂不可用", headers=extra or None) from e
    t_bars = time.monotonic()

    timings: dict = {}
    try:
        payload = engine_chart_payload.build_chart_payload(code, freq, dataset, timings=timings,
                                                           rule_profile=rule_profile, signal_scope=signal_scope,
                                                           adjust=adjust, bundle=bundle)
    except Exception as e:
        log.exception("chart calculation failed code=%s freq=%s profile=%s", code, freq, rule_profile)
        raise HTTPException(status_code=502, detail="结构计算暂不可用", headers=extra or None) from e
    # 先算 ETag 再比 If-None-Match：命中 304 不序列化响应体（ETag 的规范化序列化同样拒绝 NaN）
    etag = _chart_etag(payload)
    headers = {"ETag": etag, "Cache-Control": "no-cache", **extra}
    hit = _etag_matches(request.headers.get("if-none-match"), etag)
    body = None if hit else json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                       allow_nan=False)
    t_end = time.monotonic()
    log.info("[timing] chart code=%s freq=%s bars=%dms compute=%dms resonance=%dms total=%dms etag=%s",
             code, freq, int((t_bars - t0) * 1000), timings["compute_ms"],
             timings["resonance_ms"], int((t_end - t0) * 1000), "304" if hit else "200")
    if hit:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def _chart_history(request: Request, code: str, freq: str, before: str, limit: int,
                   adjust: str, token: str | None):
    if freq not in HISTORY_PERIODS:
        raise HTTPException(status_code=400, detail="该周期暂不支持历史分页")
    try:
        # 缺 token 视为旧页面：门面按不符处理，同样 409 并带回当前令牌
        page = engine_data.get_bars_history(code, freq, before, limit, adjust=adjust, token=token)
    except Exception as exc:
        if isinstance(exc, engine_data.TokenMismatch):
            return _conflict(token=exc.token)
        log.warning("历史分页读取失败 code=%s freq=%s", code, freq, exc_info=True)
        raise HTTPException(status_code=503, detail="历史分页不可用") from exc
    if page is None:
        raise HTTPException(status_code=503, detail="历史分页不可用")
    body = json.dumps({
        "code": code,
        "freq": freq,
        "adjust": page["adjust"],
        "kline": [{
            "time": bar["dt"],
            "open": bar["open"],
            "high": bar["high"],
            "low": bar["low"],
            "close": bar["close"],
            "volume": bar["volume"],
        } for bar in page["bars"]],
        "meta": {
            "history": True,
            **{k: page[k] for k in ("token", "has_more", "oldest_dt", "incomplete_days", "notices",
                                     "coverage", "fqf", "stale")},
        },
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    etag = 'W/"' + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


# ---------- 采集状态 ----------


@app.get("/api/status")
def api_status():
    return engine_data.status([w["code"] for w in _read_watchlist_raw()])


# ---------- 交易时段（前端自动刷新的唯一口径） ----------

_MARKET_TZ = ZoneInfo("Asia/Shanghai")   # A 股与港股同为 UTC+8


def _session_now() -> datetime:
    """东八区现在时刻（naive），与 engine/session.py 的 now 口径一致，不依赖服务器时区。"""
    return datetime.now(_MARKET_TZ).replace(tzinfo=None)


@app.get("/api/session")
def api_session():
    now = _session_now()
    if engine_data.is_demo():
        return JSONResponse(content={"mode": "demo", "checked_at": now.replace(tzinfo=_MARKET_TZ).isoformat(timespec="seconds"),
                                     "markets": {m: {"open": False} for m in ("cn", "hk")}},
                            headers={"Cache-Control": "no-store"})
    body = {"checked_at": now.replace(tzinfo=_MARKET_TZ).isoformat(timespec="seconds"),
            "markets": {m: {"open": engine_session.is_session_open(now, m)} for m in ("cn", "hk")}}
    return JSONResponse(content=body, headers={"Cache-Control": "no-store"})


# ---------- 联想搜索（自选股添加） ----------


@app.get("/api/search")
def api_search(q: str = Query("")) -> list[dict]:
    """外部联想接口：返回 [{code, name, type}]，仅 sh/sz/hk。空 q 返回 []。"""
    if engine_data.is_demo():
        return engine_data.search_samples(q)
    if engine_search is None:
        raise HTTPException(status_code=503, detail="搜索适配层未安装")
    try:
        return engine_search.search(q)
    except Exception as e:  # 上游失败统一成 502
        raise HTTPException(status_code=502, detail=f"联想搜索失败：{e}") from e


# ---------- 自选股 CRUD ----------

_WATCHLIST_LOCK = threading.RLock()


def _state_paths() -> instance_paths.InstancePaths:
    # 请求时解析：测试可逐例注入环境变量或门面 CACHE_DIR。
    return instance_paths.current(engine_data.CACHE_DIR)


def _read_watchlist_raw() -> list[dict]:
    # 文件保持 append 序；旧数据无 starred/tags 字段，归一化为 False / []
    with _WATCHLIST_LOCK:
        p = _state_paths().watchlist
        if not p.exists():
            p = instance_paths.WATCHLIST_SEED      # 种子只读：首次写入才生成个人文件
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
        path = _state_paths().watchlist
        path.parent.mkdir(parents=True, exist_ok=True)
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


# ---------- 搜索查看记录（打开事件日志 + 最近查看列表；关注状态仍以自选为唯一事实） ----------

class ViewItem(BaseModel):
    code: Annotated[str, Field(pattern=r"^(sh|sz)\d{6}$|^hk\d{5}$")]
    name: str = ""
    freq: Literal["week", "day", "m60", "m30", "m15", "m5"] = "day"
    adjust: Literal["qfq", "raw"] = "qfq"


_view_logs: dict = {}


def _view_log() -> api_view_log.ViewLog:
    """按路径缓存的查看记录（路径每次请求解析，测试可逐例注入）。"""
    path = _state_paths().view_log
    log_ = _view_logs.get(path)
    if log_ is None:
        log_ = _view_logs[path] = api_view_log.ViewLog(path)
    return log_


def _recent_views() -> dict:
    watched = {w["code"] for w in _read_watchlist_raw()}
    return {"recent": [dict(r, watched=r["code"] in watched) for r in _view_log().recent()]}


@app.get("/api/views")
def list_views() -> dict:
    return _recent_views()


@app.post("/api/views")
def record_view(item: ViewItem) -> dict:
    """页面在用户选择代码打开图表时调用一次；自动刷新不调用。"""
    _view_log().record(item.code, item.name or item.code, item.freq, item.adjust)
    return _recent_views()


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


_FACT_QUOTE_KEYS = ("price", "pct", "limit_up", "price_time", "price_label", "stale")


def _fact_quote_row(item: dict, display_row: dict | None, quote_fn) -> dict:
    """A 股行情只取门面 quote（K 线事实派生）的价格、涨跌与涨停；事实缺失或读取失败时价格为空并标
    price_unavailable，不采用显示层价格。显示层行只保留名称。"""
    code = item["code"]
    try:
        q = quote_fn(code)
    except Exception:
        log.warning("事实行情读取失败 code=%s", code, exc_info=True)
        q = None
    row = {"name": (display_row or {}).get("name") or item.get("name", "")}
    row.update({k: (q or {}).get(k) for k in _FACT_QUOTE_KEYS})
    row["limit_up"] = bool(row["limit_up"])
    row["price_unavailable"] = q is None or q.get("price") is None
    return row


def _with_fact_quotes(items: list[dict], display: dict) -> dict:
    """港股在 PH3 通过前保持显示层行，A 股由事实派生。"""
    quote_fn = engine_data.quote
    out = dict(display)
    for item in items:
        if item["code"].startswith(("sh", "sz")):
            out[item["code"]] = _fact_quote_row(item, display.get(item["code"]), quote_fn)
    return out


@app.get("/api/quotes")
def api_quotes():
    if engine_feed is None and not engine_data.is_demo():
        raise HTTPException(status_code=503, detail="行情快照适配层未安装")
    items = _read_watchlist_raw()
    try:
        codes = [it["code"] for it in items]
        r = engine_data.sample_quotes(codes) if engine_data.is_demo() else engine_feed.get_quotes(codes)
    except Exception as e:  # 数据层错误统一成 502
        raise HTTPException(status_code=502, detail="快照失败，暂不可用") from e
    quotes = _with_fact_quotes(items, r["quotes"])
    return {"missing_codes": r.get("missing_codes", []), "invalid_codes": r.get("invalid_codes", []),
            "data_version": data_identity.version([], {"quotes": quotes, "display": r.get("data_version")}),
            "meta": r.get("meta", {}), "quotes": quotes, "degraded": r["degraded"],
            "fetch_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])) if r["ts"] else None}


@app.get("/api/quote")
def api_quote(code: str = Query(..., pattern=r"^(sh|sz)\d{6}$|^hk\d{5}$")):
    """单代码报价：搜索查看的非自选代码也有卡头价格（/api/quotes 只报自选）。A 股同自选行，只取门面 quote（K 线
    事实派生），不经显示层；港股走显示层单代码报价，缓存与自选报价分开。"""
    quote_fn = engine_data.quote
    if code.startswith(("sh", "sz")) or engine_data.is_demo():
        return {"code": code, "quote": _fact_quote_row({"code": code, "name": ""}, None, quote_fn),
                "degraded": False}
    get_quote = getattr(engine_feed, "get_quote", None)
    if get_quote is None:
        raise HTTPException(status_code=503, detail="行情快照适配层未安装")
    try:
        r = get_quote(code)
    except Exception as e:  # 数据层错误统一成 502
        raise HTTPException(status_code=502, detail="报价暂不可用") from e
    return {"code": code, "quote": r["quotes"].get(code), "degraded": r["degraded"], "meta": r.get("meta", {})}


@app.get("/api/f10")
def api_f10(code: str = Query(..., pattern="^(sh|sz|hk)\\d+$")):
    if engine_feed is None and not engine_data.is_demo():
        raise HTTPException(status_code=503, detail="F10 适配层未安装")
    try:
        r = ({"f10": {}, "flow": {}, "industry_pct": None, "degraded": False,
              "ts": 0, "meta": {"mode": "demo"}} if engine_data.is_demo() else engine_feed.get_f10(code))
    except Exception as e:  # 数据层错误统一成 502
        raise HTTPException(status_code=502, detail="基本资料暂不可用") from e
    return {"data_version": r.get("data_version"), "meta": r.get("meta", {}),
            "f10": r["f10"], "flow": r["flow"], "industry_pct": r["industry_pct"],
            "degraded": r["degraded"],
            "fetch_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])) if r["ts"] else None}


# ---------- AI 完全分类 ----------
app.include_router(api_analysis.router)


# 静态托管 web/（含 index.html）
_WEB_DIR = _PKG_ROOT / "web"


@app.get("/")
def index():
    return FileResponse(_WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=_WEB_DIR), name="web")
