"""行情数据门面：只调用 engine/kline 的一致视图与采集器（取数 schema 重建，计划 B 门面契约）。

- get_bars(code, freq="day", *, adjust="qfq")：freq ∈ {week, day, m60, m30, m15, m5}，adjust ∈ {qfq, raw}，
  指数恒按 raw。视图为空（事实库没有该标的、港股供应商缓存未建）时经采集器同步取够默认窗口，仍为空抛
  DataUnavailable（API 转 502）；「该市场暂不提供」的周期（细于该市场分钟事实粒度：A 股 m15 事实下的 m5、港股 m30
  事实下的 m15/m5）不取数，原样返回空 bars 与 unsupported 提示。
  视图不足默认窗口且没有更早数据时补一次窗口，同一 (code, freq) 在 WINDOW_RETRY_S 内至多一次。
  有事实（视图非空）但落后于目标日（过了定稿时点的今天未定稿，或昨天之前缺日）或还不可服务时经采集器限时追赶：
  同步等待不超过 config.CATCHUP_WAIT_S（含本次已花的时间），超时返回现有视图、后台继续提交；采集器禁用或数据已是
  最新时不追赶；追赶后仍不可服务照常抛 DataUnavailable。
- get_bars_bundle(code, freqs, *, adjust, primary=None, with_quote=False)：一次读事务服务多周期（主图、共振与
  AI 共用），补取规则同上；给 primary（主图周期）时只有它的首取失败上抛，辅助周期首取失败只记日志；
  {freq: 返回体或 None}：视图存在而无 bar 时仍给返回体（空 bars、当前令牌），标的没有任何事实时为 None；
  with_quote 时附加 "quote" 键——同一次读事务的 quote() 报价（A 股图表应答内嵌，价格卡/自选当前行用）。
- get_bars_history(code, freq, before, limit, *, adjust, token)：先比令牌，不符抛 TokenMismatch(当前令牌)；
  标的没有任何事实返回 None。
- refetch_window(code, freq="day")：手动重拉主图周期与共振依赖的分析窗口（不改变关注状态），回报 ok/partial/failed/busy 等。
- quote(code, *, name)、status(codes)、record_calc_run(...)、start_collector(watchlist_fn)、stop_collector()。
- configure_instance()：应用生命周期；配置了实例目录时 CACHE_DIR 改由它派生（engine/instance_paths.py），退出恢复。
- 返回体：{code, freq, adjust, bars, token, source, degraded, degraded_note, fqf, fetch_time, from_cache,
  stale, stale_age_s, data_version, incomplete_days, volume_unit, coverage, notices, has_more, oldest_dt}；
  bars 元素 {dt, open, high, low, close, volume}，volume 恒为股。
- 读连接按线程各开一条（只读事务），写入一律经采集器的单写者锁。
- 导入时向 engine/session.py 注册交易日历钩子。
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from chanapp.engine import data_identity, instance_paths, session
from chanapp.engine.kline import calendar, collector, config, demo, facts, views
from chanapp.engine.kline.instance import is_demo

CACHE_DIR = instance_paths.resolve().cache_dir     # CHANAPP_CACHE_DIR，缺省仓库 .cache；实例目录在生命周期内改写
N_BARS = config.DEFAULT_WINDOW
FREQS = views.PERIODS
ADJUSTS = views.ADJUSTS
WINDOW_RETRY_S = 600          # 短窗口补取节流：新上市等确实没有更早数据的标的不每次请求都触发
_BAR_KEYS = ("dt", "open", "high", "low", "close", "volume")

_local = threading.local()
_clock = time.monotonic
_window_attempts: dict = {}   # (code, freq) → (最近补窗口时刻, 周期偏好版本)
_window_lock = threading.Lock()

log = logging.getLogger(__name__)


class DataUnavailable(Exception):
    """事实库没有可服务的数据，同步首取也没能补上。"""


class RefetchBusy(DataUnavailable):
    """同一代码的手动重拉在途，或首开等该代码的锁（历史规划、缓存刷新在途）超过 REQUEST_LOCK_WAIT_S：
    本次读取不再取数，而现有快照不可服务（API 回 503，页面受控重试）。"""


def refetching(code: str) -> bool:
    """同一代码的手动重拉是否在途：在途时读取只读现有快照，不进入首取、补窗口与追赶（它们会等重拉持有的锁）。"""
    return collector.is_enabled() and _collector().refetching(code)


class TokenMismatch(Exception):
    """分页令牌与当前视图令牌不符：数据已更新，客户端应整窗重载。"""

    def __init__(self, token: str):
        super().__init__("数据已更新")
        self.token = token


def _reader():
    """本线程的只读连接；CACHE_DIR 变化（测试隔离）时换库。"""
    path = Path(CACHE_DIR) / facts.DB_NAME
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    key = (path, is_demo())
    conn = conns.get(key)
    if conn is None:
        conn = conns[key] = facts.open_readonly(path) if is_demo() else facts.open_facts(path)
    return conn


def _collector(watchlist_fn=None) -> collector.Collector:
    return collector.shared(CACHE_DIR, watchlist_fn=watchlist_fn)


def _check(freq: str, adjust: str) -> None:
    if freq not in FREQS:
        raise ValueError(f"unsupported freq: {freq}")
    if adjust not in ADJUSTS:
        raise ValueError(f"unsupported adjust: {adjust}")


def _unsupported(view) -> bool:
    return any(n["code"] == "unsupported" for n in view.notices)


def _payload(view, *, from_cache: bool) -> dict:
    bars = [{k: b[k] for k in _BAR_KEYS} for b in view.bars]
    return {
        "code": view.code, "freq": view.freq, "adjust": view.adjust, "bars": bars,
        "token": view.token, "source": view.source,
        "degraded": view.degraded, "degraded_note": "冷备来源" if view.degraded else "",
        "fqf": view.adjust_label, "fetch_time": view.last_commit_at, "from_cache": from_cache,
        "stale": view.stale, "stale_age_s": view.stale_age_s,
        "data_version": data_identity.version(bars, {"token": view.token, "adjust": view.adjust,
                                                     "source": view.source}),
        "incomplete_days": view.incomplete_days, "volume_unit": view.volume_unit,
        "coverage": view.coverage, "notices": view.notices,
        "has_more": view.has_more, "oldest_dt": view.oldest_dt,
    }


def _servable(view) -> bool:
    return view is not None and (bool(view.bars) or _unsupported(view))


def _short(view) -> bool:
    """首页不足默认窗口且没有更早数据：历史可能还没取够。"""
    return view is not None and bool(view.bars) and len(view.bars) < N_BARS and not view.has_more


def _claim_window(code: str, freq: str, *, force: bool = False) -> bool:
    """同一 (code, freq) 在 WINDOW_RETRY_S 内只补一次窗口；首取（force）总是放行并记时。"""
    from chanapp.engine import period_preferences
    prefs = period_preferences.current()
    revision = prefs.revision if prefs is not None else None
    key, now = (code, freq), _clock()
    with _window_lock:
        last = _window_attempts.get(key)
        if not force and last is not None and last[1] == revision and now - last[0] < WINDOW_RETRY_S:
            return False
        _window_attempts[key] = (now, revision)
        return True


def _to_fetch(code: str, views_by_freq: dict) -> tuple:
    """(首取周期, 补窗口周期)：空视图总是首取，短视图按节流补取。"""
    missing = [f for f, v in views_by_freq.items() if not _servable(v)]
    short = [f for f, v in views_by_freq.items() if f not in missing and _short(v) and _claim_window(code, f)]
    for freq in missing:
        _claim_window(code, freq, force=True)
    return missing, short


def _ensure(code: str, freqs, *, required: bool) -> None:
    """required 时失败上抛（API 转 502；等锁超过期限转 RefetchBusy，API 回 503）；否则只记日志（短视图补窗口、
    主图请求里辅助周期的首取）。"""
    for freq in freqs:
        try:
            _collector().ensure_window(code, freq, wait=config.REQUEST_LOCK_WAIT_S)
        except collector.FlightBusy as exc:        # 同一代码有长任务持锁：不等它，读现有快照
            if required:
                raise RefetchBusy(f"{code} 正在更新") from exc
            log.info("补取让路 code=%s freq=%s：同一代码的取数在途", code, freq)
            return
        except Exception:
            if required:
                raise
            log.warning("补取失败 code=%s freq=%s", code, freq, exc_info=True)


def _catch_up(code: str, t0: float) -> bool:
    """有事实的视图落后于目标日或还不可服务时限时追赶（get_bars 与 get_bars_bundle 共用）：返回是否需要重读视图。

    采集器禁用时不做；数据已是最新时零请求；等待上限扣掉本次请求已花的时间（首取之后可能不再等待）；
    追赶失败只记日志：原本可服务的读取不因此变成 502，原本不可服务的照常由调用方抛 DataUnavailable。"""
    if not collector.is_enabled():
        return False
    try:
        return _collector().catch_up_bounded(code, config.CATCHUP_WAIT_S - (time.monotonic() - t0))
    except Exception:
        log.warning("追赶失败 code=%s", code, exc_info=True)
        return False


def get_bars(code: str, freq: str = "day", *, adjust: str = "qfq") -> dict:
    _check(freq, adjust)
    t0 = time.monotonic()
    view = views.read_view(_reader(), code, freq, adjust=adjust)
    if is_demo():
        if not _servable(view):
            raise DataUnavailable(f"{code} {freq} 样本暂无数据")
        return _payload(view, from_cache=True)
    if refetching(code):
        _collector().touch_viewing(code)
        if not _servable(view):
            raise RefetchBusy(f"{code} 正在重拉")
        return _payload(view, from_cache=True)
    missing, short = _to_fetch(code, {freq: view})
    from_cache = not missing
    if missing or short:
        _ensure(code, [freq], required=bool(missing))
        view = views.read_view(_reader(), code, freq, adjust=adjust)
    # 有事实但还不可服务（例如港股当天才有 raw、前复权缓存的当天归定稿）也进入同一台账约束的追赶，再读一次
    if view is not None and _catch_up(code, t0):
        fresh = views.read_view(_reader(), code, freq, adjust=adjust)
        view = fresh if _servable(fresh) else view
    if not _servable(view):
        raise DataUnavailable(f"{code} {freq} 暂无可服务数据")
    _collector().touch_viewing(code)
    log.info("[timing] bars code=%s freq=%s elapsed=%dms cache=%s", code, freq,
             int((time.monotonic() - t0) * 1000), "hit" if from_cache else "first")
    return _payload(view, from_cache=from_cache)


def _read_bundle(code: str, freqs, adjust: str, *, with_quote: bool) -> tuple[dict, dict | None]:
    if with_quote:
        return views.read_bundle_with_quote(_reader(), code, freqs, adjust=adjust)
    return views.read_bundle(_reader(), code, freqs, adjust=adjust), None


def get_bars_bundle(code: str, freqs, *, adjust: str = "qfq", primary: str | None = None,
                    with_quote: bool = False) -> dict:
    """{freq: 返回体或 None}：全部周期出自同一次读事务；空或短的周期先同步补取再整体重读。

    with_quote 时附加 "quote" 键：与全部周期同一次读事务的 quote() 口径报价（A 股价格卡与自选当前行用它，
    与末根 bar 严格同快照）；demo 为样本事实报价（样本静态、无并发写者，独立一次读取）。
    quote 不在各周期键里，不参与 _to_fetch 巡检。
    热路径一次读取，补取路径补取后整体重读一次（每次读取按周期各建一次视图）。
    primary 给定时（主图）只有该周期的首取失败上抛，其余周期首取失败记日志，按重读结果返回
    （仍空则为 None 或空 bars 的返回体）；不给时任一周期首取失败都上抛（AI 经 read_bundle 转成全 None）。
    同一代码重拉在途时只读现有快照、不补取不追赶；主图（不给 primary 时为全部周期）不可服务则抛 RefetchBusy。"""
    freqs = tuple(dict.fromkeys(freqs))
    for freq in freqs:
        _check(freq, adjust)
    t0 = time.monotonic()
    bundle, qt = _read_bundle(code, freqs, adjust, with_quote=with_quote)
    if is_demo():
        out = {f: (_payload(v, from_cache=True) if v is not None else None) for f, v in bundle.items()}
        if with_quote:
            out["quote"] = demo.quote(_reader(), code)
        return out
    if refetching(code):
        _collector().touch_viewing(code)
        needed = [bundle.get(primary)] if primary else list(bundle.values())
        if not all(_servable(v) for v in needed):
            raise RefetchBusy(f"{code} 正在重拉")
        out = {f: (_payload(v, from_cache=True) if v is not None else None) for f, v in bundle.items()}
        if with_quote:
            out["quote"] = qt
        return out
    missing, short = _to_fetch(code, bundle)
    from_cache = not missing
    if missing or short:
        required = [f for f in missing if primary is None or f == primary]
        _ensure(code, required, required=True)
        _ensure(code, [f for f in missing if f not in required], required=False)
        _ensure(code, short, required=False)
        bundle, qt = _read_bundle(code, freqs, adjust, with_quote=with_quote)
    if any(v is not None for v in bundle.values()) and _catch_up(code, t0):     # 有事实即可，同 get_bars
        bundle, qt = _read_bundle(code, freqs, adjust, with_quote=with_quote)
    _collector().touch_viewing(code)
    out = {f: (_payload(v, from_cache=from_cache) if v is not None else None) for f, v in bundle.items()}
    if with_quote:
        out["quote"] = qt
    return out


def get_bars_history(code: str, freq: str, before: str, limit: int = N_BARS, *,
                     adjust: str = "qfq", token: str | None = None) -> dict | None:
    """before 之前（不含）最多 limit 根；先比令牌（不符抛 TokenMismatch），标的没有任何事实返回 None。
    令牌相符的空页是合法的历史尽头（has_more 为假），原样返回。"""
    _check(freq, adjust)
    view = views.read_view(_reader(), code, freq, adjust=adjust, before=before, limit=limit)
    if view is None:
        return None
    if token != view.token:
        raise TokenMismatch(view.token)
    return _payload(view, from_cache=True)


def refetch_window(code: str, freq: str = "day") -> dict:
    """手动重拉：整段重取主图周期与共振依赖的分析窗口（含当天与港股缓存），不改变关注状态；失败保留已有事实。
    返回 {"status": ok | partial | failed | busy | disabled, ...}（采集器禁用时 disabled，不取数；同一代码已有重拉
    在途或在途追赶超时未结束时 busy，不取数）。"""
    if not collector.is_enabled():
        return {"status": "disabled"}
    return _collector().refetch_window(code, freq)


def quote(code: str) -> dict | None:
    """右栏与自选行情：{price, price_time, price_label(最新/昨收), pc, pct, limit_up, trade_date, stale}；
    标的没有任何事实返回 None。A 股当前代码的卡头与自选行改由 get_bars_bundle(with_quote=True) 内嵌的
    同快照报价驱动（不经此入口）。"""
    return demo.quote(_reader(), code) if is_demo() else views.quote(_reader(), code)


def search_samples(q: str) -> list[dict]:
    return demo.search(_reader(), q)


def sample_quotes(codes):
    """demo 模式的离线历史报价。"""
    quotes = {code: q for code in codes if (q := quote(code)) is not None}
    return {"quotes": quotes, "degraded": False, "ts": 0, "meta": {"mode": "demo"},
            "missing_codes": [code for code in codes if code not in quotes], "invalid_codes": []}


def status(codes) -> dict:
    if is_demo():
        return demo.status(_reader(), list(codes))
    return _collector().status(list(codes), now=datetime.now())


def _recorded_run(code, freq, input_data_version, calculation_id) -> dict | None:
    """幂等键已有非失败记录时给出与写路径相同的结果；只读，不取写者锁（事实库为 WAL，读不等写事务）。
    没有记录、记录为 failed（写路径负责恢复）或读取出错时返回 None，交写路径。"""
    try:
        row = (demo.find_calc_run(CACHE_DIR, code, freq, input_data_version, calculation_id) if is_demo()
               else facts.find_calc_run(_reader(), code, freq, input_data_version, calculation_id))
    except Exception:
        log.debug("结论历史只读预查失败，改走写路径 code=%s freq=%s", code, freq, exc_info=True)
        return None
    if row is None or row["status"] == "failed":
        return None
    return {"recorded": True, "run_id": row["run_id"], "created": False}


def record_calc_run(code: str, freq: str, *, input_start, input_end, input_data_version,
                    calculation_id, signals, source_kind="online_observed") -> dict:
    """先只读查幂等键，已记录则直接返回（重复查看不排队等采集器写者锁）；未记录才进写路径，写事务内仍复核幂等键。"""
    recorded = _recorded_run(code, freq, input_data_version, calculation_id)
    if recorded is not None:
        return recorded
    if is_demo():
        return demo.record_calc_run(CACHE_DIR, code, freq, input_start=input_start, input_end=input_end,
                                    input_data_version=input_data_version, calculation_id=calculation_id,
                                    signals=signals)
    with _collector().writer() as conn:
        return facts.record_calc_run(conn, code, freq, input_start=input_start, input_end=input_end,
                                     input_data_version=input_data_version,
                                     calculation_id=calculation_id, source_kind=source_kind,
                                     signals=signals)


def start_collector(watchlist_fn) -> bool:
    return False if is_demo() else _collector(watchlist_fn).start()


@contextmanager
def configure_instance():
    """应用启动前加载并校验部署配置、初始化实例目录；错误直接阻止启动，退出恢复缓存根。"""
    global CACHE_DIR
    from chanapp.engine.kline import instance
    settings = instance.load_instance()
    previous = CACHE_DIR
    with instance_paths.activate(settings.instance_dir, cache_dir=CACHE_DIR) as paths:
        CACHE_DIR = paths.cache_dir
        try:
            with instance.activate(settings, CACHE_DIR) as active:
                yield active
        finally:
            CACHE_DIR = previous


def stop_collector() -> None:
    if not is_demo():
        _collector().stop()


def _calendar_hook(market: str, day: date) -> bool | None:
    return calendar.is_trading_day(_reader(), market.upper(), day.isoformat())


session.set_calendar_hook(_calendar_hook)
