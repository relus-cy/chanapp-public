"""采集器：事实层唯一写者（spec §5.1、§5.3、§9 D7/D13；执行计划 A 的 A3）。

- 单写者：实例内可重入锁 + facts.writer.lock 文件锁（flock 按打开的文件描述计，同进程两个实例也互斥）；
  唯一例外是额度计数 quota_usage：必须在锁外的上游请求之前扣减，由 Budget 在自己的连接上以
  BEGIN IMMEDIATE 原子递增（WAL 下与写者事务串行），它不是行情事实，不影响任何视图代次；
- 上游请求一律在写者锁外进行，锁内只做提交、缺口与批次登记（大响应可能耗时较长）；
- 额度：按源持久化计数（quota_usage，重启不清零），非恢复请求不得占用预留额度；
- 失败：单标的退避先于单源冷却；只有连接类失败（ProviderConnectionError 与非 provider 异常）计入单源冷却，
  其余 ProviderError（5xx、越界、4xx 等）只退避该标的；ProviderUnsupported 只跳过并退还额度；
  历史来源的请求（历史追赶、请求路径追赶的续传）按 code#history 单独退避，不跳过同一代码的盘前、盘中与定稿；
  永不调用冷备（D1：冷备只能手动切换绑定）；
- 分钟历史分片的结束点截到昨天收盘槽；今天的分钟来自盘中增量（forming）、收盘定稿（closed），以及
  盘中首开/重拉/窗口追赶和盘中增量首轮前的补取（_fetch_today_closed：今天已知开市、有收盘超过
  _TODAY_SLOT_SETTLE_S 的槽且库内未覆盖时才发，至多一个请求）；
- 覆盖按槽位判定：缺口只有在提交后该区间实际可读覆盖达标（日线逐交易日有 final 行，分钟逐日覆盖会话槽位，
  隔离槽与停牌日除外）时才关闭；取回为空、全部被拒或槽位不全都计一次尝试；
- 同一缺口因标的级失败或覆盖不达标尝试 5 次后记 known_gap，不再自动重试，由 status 暴露；
  连接类失败与写失败不消耗次数；
- 跟踪与查看分开（目标 2026-09-29 第二阶段）：跟踪集合 = 自选 ∪ 系统依赖上证指数（tracked_codes）；资格按数据集
  （eligible：自选全部数据集，不在自选的上证指数只有日线，含调度线程的当日日线定稿），在每个会发请求的步骤前复核，
  不按轮快照。只有跟踪代码进入调度定稿、历史规划、缺口续传与港股缓存扩展。搜索查看的代码只维护分析窗口
  （_window_jobs/_sync_window：日线按所需交易日加余量、分钟按 WINDOW_MINUTE_FREQ 的 520 根折算，先日线后分钟，
  日线下限只认已证明的，只补缺的交易日，从新到旧、失败即停、不登记缺口、不标记已规划）；页面在看的代码
  VIEWING_WINDOW_S 租期内有盘中增量与盘前补确认（每个请求前按当前时钟复核租期）；手动重拉 refetch_window 整段重取
  主图周期与依赖窗口（含当天与港股缓存）、不改变关注状态，同一代码独占，结果按本次返回分 ok / partial / failed / busy；
- 当日定稿按（代码，交易日）调度（计划 2026-09-29 D1–D3）：首个定稿时点（A 股 20:00、港股 16:30）起，自选且
  当日未完成的代码都有资格；完成以事实库为准（当日 final 日线可读、分钟按槽位覆盖、停牌日不要求分钟、没有未裁决的
  待核验、分钟与日线核对一致），库内已完整的部分不再取数，重启后也不重取；失败按 FINALIZE_RETRY_S 节流重试
  （日历未知的日子只在旧定稿时点各折算一次），待核验是当日终态；未完成登记当日缺口并写 last_error（阶段与类别），
  完成后该日缺口逐条复核、零请求关闭；暂缓（锁忙、写者锁被占、退避、冷却、额度、绑定切换、能力不可用）这次一个
  上游请求都没发出才不计次数，已发出的照算；日历未知按定稿时点折算次数上限；
  盘中与首取写失败同样登记缺口（FactsWriteError 不得让待补任务丢失）；
- 请求路径追赶（D4）：门面读到有事实但落后或还不可服务的视图时，catch_up_bounded 在后台线程补今天（同一定稿入口与节流）
  并续传本代码近期缺口（只到昨天；非跟踪代码改为窗口补缺，至多 CATCHUP_MAX_REQUESTS 个请求），同步至多等待 CATCHUP_WAIT_S；
- 盘前参考价的确认以事实库里当日可读的有限正 pc 为准；盘中对未确认标的补取，每代码 60 秒一次；
- 历史规划：每个跟踪标的由历史追赶线程做一次日线整段回填与分钟月度缺口登记（上证指数不在自选时只做日线），
  以 settings 标记 backfill_planned / minute_planned，不依赖首次打开；之后每轮按发现水位
  discovered:<dataset>:<code> 查新缺的交易日并登记缺口（停机期间缺的日子）；
- 日线整段回填提交后按覆盖口径检查：缺可读行的日子按连续区段登记 backfill 缺口，取回过但被拒的日子逐日登记
  rejected 缺口（续传按严格口径，返回空不关闭；缺口续传中新出现的被拒日同样逐日登记），都先于日历推导登记（指数被拒的日子因此留作未知、
  不被推成休市）；取数后的覆盖从上市日起算，上市日未知才从首根起算；
  回填写失败登记缺口后返回，单个标的的失败不中断本市场余下标的；
- 港股供应商前复权缓存首建时 m30 只建默认窗口（首开同步请求可控；非跟踪代码的日线缓存也跟随窗口起点）；首开分钟周期时缓存 m30 少于该周期窗口
  即按窗口整段重取；全库续传排空分钟缺口后，raw m30 起点早于缓存起点即整段重取，把更早的历史补进缓存；
- 同一代码的首取、后台规划与供应商缓存刷新同时只有一个在途（D7 防重复请求），其余等待后读结果；
  调度线程对单代码锁只 try-acquire（锁忙本轮跳过，不阻塞）；当日定稿另有逐代码锁，调度线程与追赶互斥；
- 两个守护线程（计划 2026-09-29 D5）：调度线程（tick：盘前、盘中、定稿、日历、保活）与历史追赶线程
  （history_tick：原 BACKFILL 的规划、发现、续传与缓存扩展），慢的历史请求不阻塞盘中增量；历史追赶只处理
  跟踪代码，全库续传只在没有市场开盘时、每个缺口请求前复核资格；同一缺口至多一个线程在途（gap_id 占用），Breaker、额度与 provider 的
  懒创建都加锁。
"""
from __future__ import annotations

import bisect
import contextlib
import fcntl
import importlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
from collections import Counter, deque
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from chanapp.engine.kline import admission, bindings, calendar, config, facts, hk_vendor_qfq, sessions, views
from chanapp.engine.kline.providers.raw import (ProviderConnectionError, ProviderError,  # noqa: F401
                                                ProviderRangeError, ProviderServerError, ProviderUnsupported)
from chanapp.engine.kline.rows import FetchItem, InstrumentRow, kind_of, market_of
from chanapp.engine.kline.providers.catalog import REGISTRY

log = logging.getLogger(__name__)

_FACTORIES = {name: (spec.module, spec.factory) for name, spec in REGISTRY.items()}

_CLOSE_SLOT = {"CN": "15:00", "HK": "16:00"}
_CN_INDEX = "sh000001"
_REFETCH_WAIT_CATCHUP_S = 60                 # 手动重拉开始前等在途的请求路径追赶做完的上限
MAX_GAP_ATTEMPTS = 5
_NON_RECOVERY = ("backfill", "rejected")   # 回填类缺口的续传不占用恢复预留额度
_MAX_WINDOW_SLICES = 40
_VENDOR_RECHECK_DAYS = 30
_WINDOW_MARGIN_DAYS = 5          # 港股供应商缓存首建窗口的交易日余量（半日市、日历未知的假日）
# 定稿时点：首个是当日定稿的开始时刻（此前不取今天的日线：当前 A 股主源把返回的日线一律标 final）；
# 其余只在日历未知的日子使用（旧节奏：每个时点折算一次尝试，不按间隔重试）。A 股 20:00 起（所有者 2026-09-30：
# 当日日线 17:30、18:30 仍可能短暂返回空），按 FINALIZE_RETRY_S 当天只剩 22:00 一次重试，其余交次日历史追赶
_FINALIZE_SLOTS = {"CN": ("20:00", "22:00"), "HK": ("16:30", "18:30", "20:30")}
_LEDGER = "finalize_ledger:"                     # 定稿重试台账 settings 键前缀：finalize_ledger:<交易日>:<代码>
_VENDOR_BUILD = "vendor_build:"                  # 港股缓存自动首建的日预算：vendor_build:<日期>:<代码>
_LEDGER_FIELDS = ("attempts", "last_at", "reason", "terminal", "pending")
_REVIEW = ("pending_review", "reconcile_mismatch", "quarantined")   # 待核验：当日终态，不再自动重取
_LEGACY_MINUTE_FACT = facts.LEGACY_MINUTE_FACT    # 设置表里没有粒度记录时的分钟粒度：旧的 minute_planned:<代码> 属于它
_NEED_FETCH = ("missing_final", "missing_slots")
_VENDOR_DEFER = ("stale_binding", "frozen", "superseded")   # 供应商缓存刷新的非失败结果：定稿暂缓、不计次


def _version_key(versions) -> tuple:
    """缓存版本组的先后（两个周期的版本号，未发布记 0），用于待补证据只保留较新的一组。"""
    return tuple(versions.get(freq) or 0 for freq in sorted(versions))


def plan_key(code, kind) -> str:
    """规划标记的设置键（kind：backfill 或 minute）。分钟标记按事实粒度分开：粒度改动后新粒度重新规划回填，旧标记只读
    保留（回退时仍有效）；沿用旧键 minute_planned:<代码> 的是各市场改动前的粒度（_LEGACY_MINUTE_FACT）。"""
    if kind == "minute":
        fact = bindings.binding(market_of(code), kind_of(code), FetchItem.MINUTE_HISTORY).minute_fact_freq
        if fact and fact != _LEGACY_MINUTE_FACT[market_of(code)]:
            return f"minute_planned:{fact}:{code}"
    return f"{kind}_planned:{code}"


class CollectorLocked(RuntimeError):
    """另一个写者持有事实库写者锁。"""


class FlightBusy(Exception):
    """首开等同一代码的单飞锁超过期限（历史规划、缓存刷新或重拉在途）：调用方读现有快照，没有就报忙。"""


class _Skipped(Exception):
    """标的在退避期或源在冷却期，本轮不取。"""


class _BudgetExhausted(Exception):
    """额度不足，本轮停止。"""


class _Ineligible(Exception):
    """请求之间复核资格时代码已不再跟踪（移出自选）：不开始下一个请求。"""


def is_enabled() -> bool:
    from chanapp.engine.kline import instance
    selected = instance.current()
    return (selected is None or selected.mode == "real") and os.environ.get("COLLECTOR_ENABLED", "1") != "0"


class Budget:
    """按源计数的请求额度：每日用量写 quota_usage（进程重启不清零），每分钟用量在内存。"""

    def __init__(self, conn_factory, *, source, per_minute, per_day, reserve, clock=time.time):
        self._conn_factory, self._conn = conn_factory, None
        self.source, self.per_minute, self.per_day, self.reserve = source, per_minute, per_day, reserve
        self.clock = clock
        self._recent = deque()
        self._lock = threading.Lock()

    def _db(self):
        """调用方持 self._lock：连接懒建一次，多个线程（调度、历史追赶、请求路径追赶）经锁串行使用。"""
        if self._conn is None:
            self._conn = self._conn_factory()
        return self._conn

    def day(self) -> str:
        return date.fromtimestamp(self.clock()).isoformat()

    def used(self) -> int:
        with self._lock:
            row = self._db().execute("SELECT count FROM quota_usage WHERE source=? AND day=?",
                                     (self.source, self.day())).fetchone()
        return row["count"] if row else 0

    def ratio(self) -> float:
        return self.used() / self.per_day if self.per_day else 0.0

    def take(self, n=1, *, recovery=False) -> bool:
        with self._lock:
            now = self.clock()
            while self._recent and now - self._recent[0] >= 60:
                self._recent.popleft()
            if len(self._recent) + n > self.per_minute:
                return False
            limit = self.per_day if recovery else int(self.per_day * (1 - self.reserve))
            conn = self._db()
            with facts.write_txn(conn):
                row = conn.execute("SELECT count FROM quota_usage WHERE source=? AND day=?",
                                   (self.source, self.day())).fetchone()
                used = row["count"] if row else 0
                if used + n > limit:
                    return False
                conn.execute("INSERT INTO quota_usage(source, day, count) VALUES (?,?,?)"
                             " ON CONFLICT(source, day) DO UPDATE SET count=count+?",
                             (self.source, self.day(), n, n))
            self._recent.extend([now] * n)
            return True

    def refund(self, n=1) -> None:
        """请求没有发出（provider 在发请求前判定不支持）：退还已扣的额度。"""
        with self._lock:
            for _ in range(min(n, len(self._recent))):
                self._recent.pop()
            conn = self._db()
            with facts.write_txn(conn):
                conn.execute("UPDATE quota_usage SET count=MAX(count-?, 0) WHERE source=? AND day=?",
                             (n, self.source, self.day()))


class Breaker:
    """连败计数与冷却：key 为源名（单源冷却）或标的代码（单标的退避）。调度、历史追赶与请求路径追赶
    三类线程共用同一实例，计数的读改写加锁（D5）。"""

    def __init__(self, max_failures, cooldown_s, clock):
        self.max_failures, self.cooldown_s, self.clock = max_failures, cooldown_s, clock
        self.failures, self.until = Counter(), {}
        self._lock = threading.Lock()

    def allow(self, key) -> bool:
        with self._lock:
            return self.clock() >= self.until.get(key, 0)

    def record(self, key, ok: bool) -> None:
        with self._lock:
            if ok:
                self.failures.pop(key, None)
                self.until.pop(key, None)
                return
            self.failures[key] += 1
            if self.failures[key] >= self.max_failures:
                self.until[key] = self.clock() + self.cooldown_s
                self.failures[key] = 0


# 盘中窗口末尾的定格分钟（与 due_modes 的窗口一致）：收盘后 1 分钟取最后一根 bar 的定格
_SESSION_END_GRACE = {"CN": (("11:30:00", "11:31:00"), ("15:00:00", "15:01:00")),
                      "HK": (("12:00:00", "12:01:00"), ("16:10:00", "16:11:00"))}


_SETTLE_S = 30        # 槽边界后这么久内接口还会改刚结束的 bar（指数实测约 20–30 秒）
_TODAY_SLOT_SETTLE_S = 60   # 盘中首开补取今天已收盘槽时，只取收盘超过这么久的槽（大于上面的边界修改窗口）
_CLOSING_CUTOFF_S = 240   # 定格轮最晚到收盘后这么久（早于 A 股 15:05、港股 16:15 的收盘后续传时段）


def _closing_window(now, market):
    """now 所在的收盘定格区间 (settle, cutoff)：会话收盘后 _SETTLE_S 秒起到 _CLOSING_CUTOFF_S 秒；不在任何区间时 None。"""
    for end, _ in _SESSION_END_GRACE[market]:
        close = datetime.combine(now.date(), datetime.strptime(end, "%H:%M:%S").time())
        settle, cutoff = close + timedelta(seconds=_SETTLE_S), close + timedelta(seconds=_CLOSING_CUTOFF_S)
        if close <= now <= cutoff:
            return settle, cutoff
    return None


def _closing_round(now, market, last) -> bool:
    """收盘定格轮：上午、下午收盘后 _SETTLE_S 秒起，上一轮早于该时刻就再取一轮（不受盘中节流），免得节流让最后
    一根停在收盘竞价前的形成值直到定稿。按「上一轮开始时刻」判，每个会话末尾只补这一轮；上一轮拖过定格分钟
    （慢请求、多代码串行）时在定格分钟之后、_CLOSING_CUTOFF_S 之前补上。当天还没有过盘中轮时不补。"""
    window = _closing_window(now, market)
    return bool(window and last is not None and window[0] <= now and last < window[0])


def due_modes(now, *, market, is_trading_day, state) -> list:
    """本轮应执行的取数模式（纯函数）。state：preopen_done、last_preopen、last_intraday、quota_ratio、
    calendar_date、keepalive_date。

    FINALIZE 从首个定稿时点（A 股 20:00、港股 16:30）起到当日结束每轮都给：哪些代码该定稿、何时重试由
    Collector.finalize_due 按（代码，交易日）与事实库判定（计划 2026-09-29 D1），不再按市场锁存。
    BACKFILL 只表示全库续传的时段：由历史追赶线程（history_tick）读取，调度线程不执行（D5）。"""
    hhmm = now.strftime("%H:%M")
    today = now.date().isoformat()
    modes = []
    if not is_trading_day:
        modes.append("BACKFILL")
    else:
        last_pre = state.get("last_preopen")
        if (market == "CN" and "09:16" <= hhmm <= "09:29" and not state.get("preopen_done")
                and (last_pre is None or (now - last_pre).total_seconds() >= 60)):
            modes.append("PREOPEN")                   # 每分钟至多一轮，只重试未确认的标的
        # 窗口含收盘后 1 分钟（取最后一根 bar 的定格），按秒比较：11:31:30 已在窗口外
        windows = ((("09:30:00", "11:31:00"), ("13:00:00", "15:01:00")) if market == "CN"
                   else (("09:30:00", "12:01:00"), ("13:00:00", "16:11:00")))
        hms = now.strftime("%H:%M:%S")
        last = state.get("last_intraday")
        if any(start <= hms <= end for start, end in windows):
            interval = config.INTRADAY_INTERVAL_HK_S if market == "HK" else config.INTRADAY_INTERVAL_S
            if state.get("quota_ratio", 0.0) > config.QUOTA_SLOWDOWN_RATIO:
                interval *= 2
            if last is None or (now - last).total_seconds() >= interval or _closing_round(now, market, last):
                modes.append("INTRADAY")
        elif _closing_round(now, market, last):          # 定格分钟里没轮到（上一轮拖过了窗口）：截止前补一轮
            modes.append("INTRADAY")
        if hhmm >= _FINALIZE_SLOTS[market][0]:
            modes.append("FINALIZE")
        if hhmm >= ("15:05" if market == "CN" else "16:15"):
            modes.append("BACKFILL")
    if hhmm >= "08:30" and state.get("calendar_date") != today:
        modes.append("CALENDAR")
    if now.weekday() == 5 and hhmm >= "10:00" and state.get("keepalive_date") != today:
        modes.append("KEEPALIVE")
    return modes


class Collector:
    def __init__(self, cache_dir, *, providers=None, clock=None, watchlist_fn=None, minute_enabled=None):
        self.cache_dir = Path(cache_dir)
        self.providers = dict(providers or {})
        # 显式注入 provider（测试与回放）时不再懒加载真实源：未注入的源按不支持跳过，不打外网
        self._lazy = providers is None
        self.clock = clock or time.time
        self.watchlist_fn = watchlist_fn or (lambda: [])
        # 实例状态提供实时 predicate；直接库调用默认启用，保持既有取数契约。
        self.minute_enabled = minute_enabled or (lambda code: True)
        self._local = threading.local()
        self._rlock = threading.RLock()
        self._viewing: dict = {}
        self._today_prefilled: dict = {}   # {交易日: {code}}：盘中增量首轮前已补过（或无需补）今天已收盘槽
        self._flights: dict = {}
        self._vendor_extended: dict = {}   # 港股供应商缓存：已为之触发过扩展的 raw m30 起点
        self._vendor_stale_pending: dict = {}   # 已证实重述、标 stale 时写者锁被占的代码 → 取数时的版本（锁释放后补落盘）
        self._pending_guard = threading.Lock()   # 待补表的登记、快照与条件删除（历史线程、调度线程、请求路径并发）
        self._vendor_widened: dict = {}    # 港股供应商缓存：按所请求周期补窗口已试过的最早起点（及日线扩展的 raw 起点）
        self._flights_guard = threading.Lock()
        self._budgets: dict = {}
        self._providers_guard = threading.Lock()   # 懒创建 provider 与额度计数：多线程下先查后建只建一次（D5）
        self._budgets_guard = threading.Lock()
        self._gap_claims: set = set()              # 正在续传的缺口 gap_id：同一缺口至多一个线程在途（F24）
        self._claims_guard = threading.Lock()
        self._state: dict = {}
        # 当日定稿的逐代码记录 (code, 交易日) → 尝试次数、上次尝试、最近原因、完成与终态（内存；重启清空，
        # 完成与否以事实库为准，所以清空只会多一次零请求的判定）
        self._finals: dict = {}
        self._finals_guard = threading.Lock()
        self._final_locks: dict = {}       # 当日定稿的逐代码锁：调度线程与请求路径追赶互斥（都只 try-acquire）
        self._catchups: dict = {}          # 请求路径追赶：code → 完成事件（在途判定）
        self._refetching: set = set()      # 手动重拉在途的代码：请求路径追赶让路（重拉已覆盖窗口与当天）
        self._catchup_at: dict = {}        # 请求路径追赶：code → 最近一次启动的时钟读数
        self._catchup_guard = threading.Lock()
        self.source_breaker = Breaker(config.BREAKER_MAX_FAILURES, config.BREAKER_COOLDOWN_S, self.clock)
        self.code_backoff = Breaker(config.CODE_BACKOFF_AFTER, config.CODE_BACKOFF_S, self.clock)
        self._thread = None
        self._history_thread = None
        self._stop = threading.Event()

    # ---- 基础设施 ----
    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock())

    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            conn = self._local.conn = facts.open_facts(self.cache_dir / facts.DB_NAME)
        return conn

    def _lock_held(self) -> bool:
        return getattr(self._local, "lock_depth", 0) > 0

    @contextmanager
    def writer(self):
        with self._rlock:
            if self._lock_held():
                self._local.lock_depth += 1
                try:
                    yield self.conn()
                finally:
                    self._local.lock_depth -= 1
                return
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            handle = open(self.cache_dir / facts.WRITER_LOCK_NAME, "a+")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise CollectorLocked("事实库写者锁已被占用") from None
            self._local.lock_depth = 1
            try:
                yield self.conn()
            finally:
                self._local.lock_depth = 0
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()

    def provider(self, name):
        with self._providers_guard:
            if name not in self.providers:
                if not self._lazy:
                    raise ProviderUnsupported(f"{name} 未注入")
                module, cls = _FACTORIES[name]
                self.providers[name] = getattr(importlib.import_module(module), cls)()
            return self.providers[name]

    def _budget(self, source):
        quota = config.QUOTA.get(source)
        if quota is None:
            return None
        with self._budgets_guard:            # 两个 Budget 实例会各记各的每分钟用量
            if source not in self._budgets:
                self._budgets[source] = Budget(lambda: facts.open_facts(self.cache_dir / facts.DB_NAME),
                                               source=source, per_minute=quota["per_minute"],
                                               per_day=quota["per_day"], reserve=quota["reserve"],
                                               clock=self.clock)
            return self._budgets[source]

    @contextmanager
    def _history_origin(self):
        """本线程接下来的上游请求来自历史追赶（历史线程的规划与续传、请求路径追赶的续传）：单标的退避改记
        code#history，历史连败不跳过同一代码的盘前、盘中与定稿（舱壁隔离）；单源冷却仍共用。"""
        prev = getattr(self._local, "history", False)
        self._local.history = True
        try:
            yield
        finally:
            self._local.history = prev

    def _backoff_key(self, code) -> str:
        return f"{code}#history" if getattr(self._local, "history", False) else code

    def _call(self, code, source, fn, *, recovery=False, capability=None, on_sent=None, minute=False):
        """一次上游请求（写者锁外）：退避与冷却判定、额度、失败归类。单标的退避的键见 _history_origin。

        on_sent 给定时，请求进入实际调用（fn）后必调一次：返回、上游报错与中断（KeyboardInterrupt 等，结果未知）
        都算发出；进入之前的跳过（退避、冷却、额度、provider 不可用或不提供该能力）与 fn 报 ProviderUnsupported
        （能力未验证，与额度一样退还）不算。"""
        if self._lock_held():
            raise RuntimeError("上游请求不得在写者锁内进行")
        if minute and not self.minute_enabled(code):
            raise _Skipped()
        key = self._backoff_key(code)
        if not self.code_backoff.allow(key) or not self.source_breaker.allow(source):
            raise _Skipped()
        try:
            provider = self.provider(source)
        except ProviderUnsupported:
            raise
        except Exception as exc:  # noqa: BLE001 — SDK 缺失或凭证未配置：计入单源冷却
            self.source_breaker.record(source, False)
            raise ProviderConnectionError(f"{source} 初始化失败: {type(exc).__name__}") from None
        if capability and not callable(getattr(provider, capability, None)):
            raise ProviderUnsupported(f"{source} 不提供 {capability}")   # 不占额度、不计失败
        budget = self._budget(source)
        if budget is not None and not budget.take(recovery=recovery):
            raise _BudgetExhausted()
        if minute and not self.minute_enabled(code):
            if budget is not None:
                budget.refund()
            raise _Skipped()
        unsupported = False
        try:
            out = fn(provider)
        except ProviderUnsupported:
            unsupported = True
            if budget is not None:
                budget.refund()
            raise
        except ProviderConnectionError:
            self.code_backoff.record(key, False)
            self.source_breaker.record(source, False)
            raise
        except ProviderError:
            self.code_backoff.record(key, False)        # 5xx、越界、4xx：标的级，不冷却整个源
            raise
        except Exception as exc:  # noqa: BLE001 — 未归类的异常按连接类处理
            self.code_backoff.record(key, False)
            self.source_breaker.record(source, False)
            raise ProviderConnectionError(f"{type(exc).__name__}: {exc}") from None
        finally:
            if on_sent is not None and not unsupported:
                on_sent()
        self.code_backoff.record(key, True)
        self.source_breaker.record(source, True)
        return out

    # today 取自本轮模式的 now（调度传入），缺省才用时钟：准入的越界判定以它为准
    def _commit_day(self, conn, code, rows, *, item, source, gen, start=None, end=None, now=None):
        return facts.commit_day_rows(conn, rows, market=market_of(code), kind=kind_of(code),
                                     item=item, source=source, binding_gen=gen,
                                     today=(now or self.now()).date().isoformat(), start=start, end=end)

    def _commit_minutes(self, conn, code, rows, *, item, fact, source, gen, start=None, end=None, now=None):
        return facts.commit_minute_rows(conn, rows, market=market_of(code), kind=kind_of(code),
                                        item=item, fact_freq=fact, source=source, binding_gen=gen,
                                        today=(now or self.now()).date().isoformat(), start_slot=start,
                                        end_slot=end)

    def _fact_freq(self, code):
        # 偏好按请求实时读取：取消分钟后保留事实与缺口，恢复后沿用窗口及历史回填。
        if not self.minute_enabled(code):
            return None
        return bindings.binding(market_of(code), kind_of(code), FetchItem.MINUTE_HISTORY).minute_fact_freq

    def _yesterday(self, code) -> str:
        today = self.now().date().isoformat()
        row = self.conn().execute("SELECT MAX(date) AS d FROM calendar WHERE market=? AND is_open=1"
                                  " AND date<?", (market_of(code), today)).fetchone()
        return row["d"] or (self.now().date() - timedelta(days=1)).isoformat()

    def _start(self, code, *, years, lookup=True) -> str:
        """取数起点：年限下限与上市日取晚者。lookup 为假时上市日只读本地（只读判断不联网）。"""
        today = self.now().date()
        floor = (config.HK_DAY_BACKFILL_FROM if market_of(code) == "HK" and years == config.DAY_BACKFILL_YEARS
                 else today.replace(year=today.year - years).isoformat())
        listed = self._list_date(code) if lookup else self._known_list_date(code)
        start = max(floor, listed) if listed else floor
        if years == config.MINUTE_BACKFILL_YEARS and self._planned(code, "backfill"):
            # 日线整段回填已完成且无未决缺口时，首根日线之前没有交易：分钟不往前登记（次新股、上市日未知）
            first = self.conn().execute("SELECT MIN(trade_date) FROM day_bars WHERE code=?", (code,)).fetchone()[0]
            if first and not facts.open_gaps(self.conn(), code, "day"):
                start = max(start, first)
        return start

    def _plan_key(self, code, kind) -> str:
        return plan_key(code, kind)

    def _planned(self, code, kind) -> bool:
        return facts.setting(self.conn(), self._plan_key(code, kind)) is not None

    def _mark_planned(self, conn, code, kind) -> None:
        if kind == "minute" and not self.minute_enabled(code):
            return
        with facts.write_txn(conn):
            facts.set_setting(conn, self._plan_key(code, kind), self.now().date().isoformat())

    def _current_gap(self, gap) -> bool:
        """缺口属于现行数据集：日线或本代码现行分钟事实粒度。粒度改动后旧粒度的缺口原样保留（回退时仍可续传），
        但不再驱动请求、不算落后。"""
        return gap["dataset"] == "day" or gap["dataset"] == self._fact_freq(gap["code"])

    def sync_minute_fact_freq(self) -> list:
        """分钟事实粒度改动（bindings.BINDINGS 的 minute_fact_freq）后的一次性切换，启动时执行、幂等：对粒度与
        设置表记录不同的绑定推进代次（保持当前生效源），旧代次的迟到批次因此拒写、视图令牌随之变化；再记下新粒度。
        只日线以 day 记录停用状态，停用与恢复都推进代次；旧分钟事实与缺口原样保留。
        没有记录时按 _LEGACY_MINUTE_FACT 视为改动前的粒度。返回本次推进的 (市场, 类型, 取数项)。"""
        bumped = []
        with self.writer() as conn:
            for b in bindings.BINDINGS:
                if b.item not in (FetchItem.MINUTE_HISTORY, FetchItem.MINUTE_LIVE):
                    continue
                current = b.minute_fact_freq or "day"
                key = f"minute_fact_freq:{b.market}:{b.kind}:{b.item.value}"
                recorded = facts.setting(conn, key) or _LEGACY_MINUTE_FACT[b.market]
                if recorded != current:
                    source = bindings.active(conn, b.market, b.kind, b.item)[0]
                    bindings.switch(conn, b.market, b.kind, b.item, source,
                                    reason=f"minute_fact_freq {recorded}->{current}")
                    bumped.append((b.market, b.kind, b.item.value))
                    log.warning("分钟事实粒度切换 %s/%s/%s %s→%s，绑定代次已推进", b.market, b.kind, b.item.value,
                                recorded, current)
                if facts.setting(conn, key) != current:
                    with facts.write_txn(conn):
                        facts.set_setting(conn, key, current)
        return bumped

    @contextmanager
    def _single_flight(self, code):
        """同一代码的首取与历史规划同时只有一个在途（spec D7 防重复请求）：后来者等它完成，
        再按已落库的结果继续（已规划的整段取数不会重做）。上游请求仍在写者锁外，因此不得在写者锁内等待。
        本线程设了首开等锁期限（ensure_window 的 wait）时，等不到就抛 FlightBusy，不另行取数。"""
        if self._lock_held():
            raise RuntimeError("不得在写者锁内等待同一代码的首取")
        with self._flights_guard:
            lock = self._flights.setdefault(code, threading.RLock())
        deadline = getattr(self._local, "flight_deadline", None)
        if deadline is None:
            lock.acquire()
        elif not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise FlightBusy(code)
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def _bounded_flight(self, code, wait):
        """请求路径（手动重拉）取同一代码的单飞锁：至多等 wait 秒，等不到抛 FlightBusy；期限只管这一次取锁，
        拿到之后的嵌套取锁照常等待（重拉已开始取数，不能在中途被打断）。"""
        prev = getattr(self._local, "flight_deadline", None)
        self._local.flight_deadline = time.monotonic() + wait
        try:
            with self._single_flight(code):
                self._local.flight_deadline = prev        # 拿到锁：之后照常等待
                yield
        finally:
            self._local.flight_deadline = prev

    @contextmanager
    def _try_flight(self, code, table=None):
        """调度线程用的单飞锁：不等待，锁忙（同一代码的首取、规划或缓存刷新在途）时产出 False，本轮跳过该代码。
        table 给 self._final_locks 时取当日定稿锁（调度线程与请求路径追赶之间互斥）。"""
        if self._lock_held():
            raise RuntimeError("不得在写者锁内取同一代码的单飞锁")
        with self._flights_guard:
            lock = (self._flights if table is None else table).setdefault(code, threading.RLock())
        if not lock.acquire(blocking=False):
            yield False
            return
        try:
            yield True
        finally:
            lock.release()

    def _plan_history(self, code) -> None:
        """历史规划：首次做日线整段回填（失败则登记缺口）与分钟按月缺口登记；之后每轮发现新缺的交易日；
        港股另建供应商前复权缓存。planned 标记只表示首次整段规划已登记，不阻止之后的发现。
        只作系统依赖（不在自选）的上证指数只规划日线：日历推导需要它的 final 日线，不需要分钟。
        每个会发请求的步骤前复核资格：本轮中途移出自选的代码不再开始新的回填与缓存工作（已发出的请求照常提交）。"""
        if not self.eligible(code):
            return
        if not self._planned(code, "backfill"):
            self.backfill_day(code)
        if not self.is_tracked(code):
            if code == _CN_INDEX and self._planned(code, "backfill"):
                self.discover_missing(code, datasets=("day",))
            return
        if self._planned(code, "backfill") and not self._planned(code, "minute") and self._fact_freq(code):
            self.plan_minute_backfill(code)
            with self.writer() as conn:
                self._mark_planned(conn, code, "minute")
        else:
            self.discover_missing(code)
        if self.is_tracked(code):
            self._history_first_build(code)

    def _history_first_build(self, code) -> None:
        """历史线程为港股自选首建供应商前复权缓存（已建成的缓存由定稿与续传刷新，这里不管）。

        交易日（日历未知按工作日）过了首个定稿时点就让位给定稿：缓存没追上时定稿本来就重试缓存，次数记在当日台账。
        首建与其他非定稿的自动刷新一样截止日不覆盖当天（refresh_vendor_qfq 的当天规则，截止日在请求开始时定），
        所以时点前开始、跨过时点仍在途的首建也不请求当天。
        时点前与非交易日首建自有持久日预算：每天至多 FINALIZE_MAX_ATTEMPTS 次，失败后按失败时的时钟隔
        FINALIZE_RETRY_S；供应商「成功」返回空集或截短序列时上游调用不算失败、不触发退避，没有预算就会每轮重取。
        次数在请求前先落盘（写不进就不发请求，重启不清零）；一个供应商请求都没发出就暂缓（退避、冷却、额度、绑定切换、
        冷备冻结、移出自选）时退还。打开页面时的首建（_ensure_vendor）由用户动作触发，不走这里。"""
        if market_of(code) != "HK" or kind_of(code) != "stock":
            return
        conn = self.conn()
        if hk_vendor_qfq.read(conn, code, "day")[1] is not None:
            return
        if self._finalize_open("HK", self.now())[0]:
            return
        day = self.now().date().isoformat()
        key = f"{_VENDOR_BUILD}{day}:{code}"
        prev = facts.setting(conn, key) or {"attempts": 0, "last_at": None}
        if prev["attempts"] >= config.FINALIZE_MAX_ATTEMPTS or (
                prev["last_at"] is not None and self.clock() - prev["last_at"] < config.FINALIZE_RETRY_S):
            return
        if not self._save_budget(key, {"attempts": prev["attempts"] + 1, "last_at": self.clock()}, _VENDOR_BUILD,
                                 day):
            return
        sent = []
        try:
            res = self.refresh_vendor_qfq(code, self.now(), eligible=self.is_tracked, on_sent=lambda: sent.append(1))
        except BaseException:
            if not sent:
                self._save_budget(key, prev, _VENDOR_BUILD, day)                  # 没发出请求：退还
            raise
        if hk_vendor_qfq.read(self.conn(), code, "day")[1] is not None:
            return
        if not sent and ("untracked" in res or res.get("deferred") or any(res.get(k) for k in _VENDOR_DEFER)):
            self._save_budget(key, prev, _VENDOR_BUILD, day)                      # 没真正试成：退还
        else:
            self._save_budget(key, {"attempts": prev["attempts"] + 1, "last_at": self.clock()}, _VENDOR_BUILD, day)

    def _save_budget(self, key, value, prefix, day) -> bool:
        """持久写一条按日预算（同一事务删掉更早日期的同类记录）；写不进（写者锁被别的进程占用、磁盘错误）返回 False。"""
        try:
            with self.writer() as conn, facts.write_txn(conn):
                facts.set_setting(conn, key, value)
                conn.execute("DELETE FROM settings WHERE key LIKE ? AND substr(key, ?, 10) < ?",
                             (f"{prefix}%", len(prefix) + 1, day))
            return True
        except (CollectorLocked, facts.FactsWriteError, sqlite3.Error):
            log.warning("按日预算写入失败 key=%s", key, exc_info=True)
            return False

    def _list_date(self, code):
        row = self.conn().execute("SELECT list_date FROM instruments WHERE code=?", (code,)).fetchone()
        if row:
            return row["list_date"]
        if kind_of(code) != "stock" or market_of(code) != "CN":
            return None
        source, _ = bindings.active(self.conn(), "CN", "stock", FetchItem.INSTRUMENT_LIST)
        try:
            # 上市日查询单独计退避（与日历查询同样不借标的的键）：元数据查询失败只让上市日按未知处理，
            # 不能累计到该标的的数据请求退避上（一次首开会查两次，两次失败就会把紧接着的窗口取数跳过）
            info = self._call(f"{code}#instrument", source, lambda p: p.instrument(code), capability="instrument")
        except (_Skipped, _BudgetExhausted, ProviderError):
            return None
        if isinstance(info, InstrumentRow):
            with self.writer() as conn, facts.write_txn(conn):
                conn.execute("INSERT OR REPLACE INTO instruments(code, name, list_date, delist_date, kind,"
                             " source, fetched_at) VALUES (?,?,?,?,?,?,?)",
                             (code, info.name, info.list_date, info.delist_date, info.kind, source,
                              facts.now_iso()))
            return info.list_date
        return None

    # ---- 取数模式 ----
    def backfill_day(self, code) -> dict:
        market, kind = market_of(code), kind_of(code)
        source, gen = bindings.active(self.conn(), market, kind, FetchItem.DAY_HISTORY)
        start, end = self._start(code, years=config.DAY_BACKFILL_YEARS), self._yesterday(code)
        try:
            rows = self._call(code, source, lambda p: p.day_history(code, start, end))
        except (_Skipped, ProviderUnsupported):
            return {"skipped": True}
        except _BudgetExhausted:
            return {"budget_exhausted": True}
        except ProviderError as exc:
            with self.writer() as conn:
                with facts.write_txn(conn):
                    facts.record_gap(conn, code, "day", start, end, "backfill")
                self._mark_planned(conn, code, "backfill")        # 已规划：余下交给缺口续传
            return {"error": str(exc)}
        with self.writer() as conn:
            try:
                res = self._commit_day(conn, code, rows, item=FetchItem.DAY_HISTORY.value, source=source,
                                       gen=gen, start=start, end=end)
            except facts.StaleBinding:
                log.info("绑定已切换，丢弃迟到日线批次 code=%s", code)
                return {"stale_binding": True}
            except facts.FactsWriteError:
                # 登记待补后返回，不抛出：本市场余下标的的规划、缺口排空与缓存扩展照常进行
                log.warning("日线回填写入失败，登记待补 code=%s", code, exc_info=True)
                self._record_gap_safely(conn, code, "day", start, end, "write_failed")
                try:
                    self._mark_planned(conn, code, "backfill")
                except (facts.FactsWriteError, sqlite3.Error):
                    log.error("回填规划标记写入失败 code=%s", code, exc_info=True)
                return {"error": "write_failed"}
            # 先登记未覆盖或被拒的日子、再推日历：指数被拒的日子有缺口，不会被推成休市。
            # 取回过但被拒的日子逐日记 rejected（续传按严格口径，返回空不关闭；逐日登记不把中间日历未知的
            # 假日并进必须覆盖的区段），只是没返回的按连续区段记 backfill
            returned = {r.trade_date for r in rows}
            missing, have = self._day_missing(code, start, end, unknown_required=False, from_first_row=True,
                                              returned=returned)
            rejected = [d for d in missing if d in returned]
            plain = [d for d in missing if d not in returned]
            ranges = ([(day, day, "rejected") for day in rejected]
                      + [(lo, hi, "backfill") for lo, hi in self._missing_ranges(plain, have | set(rejected))])
            with facts.write_txn(conn):
                for lo, hi, reason in ranges:
                    facts.record_gap(conn, code, "day", lo, hi, reason)
            self._derive_calendar(conn, code)
            self._reconcile(conn, code, [r.trade_date for r in rows])
            self._note_day_absent(conn, code, start, end, rows)
            self._mark_planned(conn, code, "backfill")
        return {"inserted": res.inserted, "revised": res.revised, "gaps": len(ranges)}

    def commit_import(self, code, day_rows, minute_rows, *, source, end_day) -> dict:
        """一次性导入（engine/kline/ingest.py）：规范行经同一准入与提交写入，之后推日历、核对分钟与日线，
        与回填、缺口续传的提交后步骤相同。不发上游请求、不标记规划；除下面的指数衔接段外不登记缺口。
        上证指数：文件内的指数区间视为连续（预检已查空档），与库里已有指数日线之间不相接的一段没有连续证据，
        先登记 backfill 缺口再推日历，使其留作未知、交给采集器续传，而不是被推成休市。
        返回 {数据集: CommitResult}；绑定在写者锁内取当前代次。"""
        market, kind = market_of(code), kind_of(code)
        out = {}
        with self.writer() as conn:
            if day_rows:
                _, gen = bindings.active(conn, market, kind, FetchItem.DAY_HISTORY)
                seams = self._import_index_seams(conn, code, day_rows)
                out["day"] = self._commit_day(conn, code, day_rows, item=FetchItem.DAY_HISTORY.value,
                                              source=source, gen=gen, end=end_day)
                with facts.write_txn(conn):
                    for lo, hi in seams:
                        facts.record_gap(conn, code, "day", lo, hi, "backfill")
                self._derive_calendar(conn, code)
            if minute_rows:
                fact = self._fact_freq(code)
                _, gen = bindings.active(conn, market, kind, FetchItem.MINUTE_HISTORY)
                out[fact] = self._commit_minutes(conn, code, minute_rows, item=FetchItem.MINUTE_HISTORY.value,
                                                 fact=fact, source=source, gen=gen, end=f"{end_day} 23:59")
            self._reconcile(conn, code, [r.trade_date for r in (*day_rows, *minute_rows)])
        return out

    def commit_import_metadata(self, calendar_rows, instruments) -> None:
        """随包样本的已校验辅助输入；仍由采集器单写者提交，重复输入不改内容。"""
        with self.writer() as conn, facts.write_txn(conn):
            calendar.store_rows(conn, calendar_rows, source="import")
            for row in instruments:
                conn.execute("INSERT INTO instruments(code, name, list_date, delist_date, kind, source, fetched_at)"
                             " VALUES (?,?,?,?,?,'import',?) ON CONFLICT(code) DO NOTHING",
                             (row.code, row.name, row.list_date, row.delist_date, row.kind, facts.now_iso()))

    @staticmethod
    def _import_index_seams(conn, code, day_rows) -> list:
        """导入的指数区间与库里已有指数 final 日线之间、日历未知的工作日段 [(lo, hi)]（至多首尾两段）。"""
        if code != _CN_INDEX:
            return []
        days = sorted(r.trade_date for r in day_rows if not r.sf)
        if not days:
            return []
        first, last = days[0], days[-1]
        bounds = conn.execute(
            "SELECT (SELECT MAX(trade_date) FROM current_day_bars WHERE code=? AND provenance='final' AND sf=0"
            " AND trade_date<?) AS before, (SELECT MIN(trade_date) FROM current_day_bars WHERE code=?"
            " AND provenance='final' AND sf=0 AND trade_date>?) AS after", (code, first, code, last)).fetchone()
        seams = []
        for a, b in ((bounds["before"], first), (last, bounds["after"])):
            if a is None or b is None:
                continue
            day, end, unknown = date.fromisoformat(a) + timedelta(days=1), date.fromisoformat(b), []
            while day < end:
                iso = day.isoformat()
                if day.weekday() < 5 and conn.execute("SELECT 1 FROM calendar WHERE market='CN' AND date=?",
                                                      (iso,)).fetchone() is None:
                    unknown.append(iso)
                day += timedelta(days=1)
            if unknown:
                seams.append((unknown[0], unknown[-1]))
        return seams

    @staticmethod
    def _derive_calendar(conn, code) -> None:
        """过去交易日：A 股由上证指数 final 日线推出，港股由港股 final 日线推出（calendar 模块）。"""
        if code == _CN_INDEX:
            with facts.write_txn(conn):
                calendar.derive_cn_past(conn)
        elif market_of(code) == "HK":
            with facts.write_txn(conn):
                calendar.derive_hk_past(conn)

    def _month_slices(self, code, start_day: str, end_day: str):
        """[start_day, end_day] 按自然月从新到旧切片，结束点截到 end_day 的收盘槽。"""
        close = _CLOSE_SLOT[market_of(code)]
        first = date.fromisoformat(start_day)
        cursor = date.fromisoformat(end_day)
        while cursor >= first:
            month_start = cursor.replace(day=1)
            lo = max(month_start, first)
            yield f"{lo.isoformat()} 09:30", f"{cursor.isoformat()} {close}", lo, cursor
            cursor = month_start - timedelta(days=1)

    # ---- 覆盖判定（spec §5.3「完整性单独登记」：按槽位，而不是「有一根就算」）----
    @staticmethod
    def _dates(lo: str, hi: str):
        day, last = date.fromisoformat(lo[:10]), date.fromisoformat(hi[:10])
        while day <= last:
            yield day.isoformat()
            day += timedelta(days=1)

    @staticmethod
    def _expected(status, day, *, unknown_required) -> bool:
        if status is None:
            return unknown_required and date.fromisoformat(day).weekday() < 5
        return status

    def _expected_day(self, market, day, *, unknown_required) -> bool:
        """日历已知开市日；日历未知的工作日只在 unknown_required 时计入（规划与取数前按应有处理）。"""
        return self._expected(calendar.is_trading_day(self.conn(), market, day), day,
                              unknown_required=unknown_required)

    def _required_slots(self, market, fact, day) -> set:
        return calendar.required_slots(self.conn(), market, fact, day)

    def _minute_covered(self, code, fact, lo: str, hi: str, *, unknown_required=True, returned=()) -> bool:
        """[lo, hi] 内每个应有分钟的交易日，已收盘可读槽位覆盖该日全部会话槽位（隔离槽不要求）。

        停牌日（日线 sf=1）不要求分钟。unknown_required=False 用于取数之后：只要求日历已知的开市日、
        可读有行的日子与本次取回过行（含被拒）的日子；日历未知的工作日取回为空按非交易日处理（港股假日）。"""
        return not self._minute_uncovered_days(code, fact, lo, hi, unknown_required=unknown_required,
                                               returned=returned)

    def _minute_uncovered_days(self, code, fact, lo: str, hi: str, *, unknown_required=True, returned=()) -> list:
        """[lo, hi] 内分钟未覆盖的日子（升序，口径见 _minute_covered）。整段一次取隔离键、已收盘槽位、日线与日历，
        查询条数不随天数增长；逐日调用 _minute_covered(d, d) 得到的是同一集合。"""
        conn, market = self.conn(), market_of(code)
        hidden = facts.quarantined_keys(conn, code, fact)
        present: dict = {}
        for r in conn.execute("SELECT trade_date, slot_end FROM current_minute_bars WHERE code=? AND fact_freq=?"
                              " AND state='closed' AND trade_date>=? AND trade_date<=?",
                              (code, fact, lo[:10], hi[:10])):
            if r["slot_end"] not in hidden:
                present.setdefault(r["trade_date"], set()).add(r["slot_end"])
        suspended = {r["trade_date"] for r in facts.read_day_rows(conn, code, lo[:10], hi[:10])
                     if r["sf"] == 1 and r["provenance"] != "preopen"}
        days = [d for d in self._dates(lo, hi) if d not in suspended]
        span = calendar.Span(conn, market, days[0], days[-1]) if days else None
        out = []
        for day in days:
            if (day not in present and day not in returned
                    and not self._expected(span.is_trading_day(day), day, unknown_required=unknown_required)):
                continue
            if not (span.required_slots(fact, day) - hidden) <= present.get(day, set()):
                out.append(day)
        return out

    def _known_list_date(self, code):
        """只读本地 instruments（写者锁内也可调用，不打上游）。"""
        row = self.conn().execute("SELECT list_date FROM instruments WHERE code=?", (code,)).fetchone()
        return row["list_date"] if row else None

    def _day_missing(self, code, lo: str, hi: str, *, unknown_required=True, from_first_row=False,
                     returned=(), exclude=()) -> tuple:
        """[lo, hi] 内缺可读 final 日线的应有交易日（升序）与可读日期集合。

        取数之后（from_first_row）：上市日已知时从上市日与 lo 较晚者起算，未知时首根之前视为未上市；
        本次取回过行（含被拒）且在区间内的日子必须有可读行；exclude 中的日子由单日严格缺口承接，不计。"""
        lo, hi = lo[:10], hi[:10]
        have = {r["trade_date"] for r in facts.read_day_rows(self.conn(), code, lo, hi)
                if r["provenance"] == "final"}
        first = lo
        if from_first_row:
            listed = self._known_list_date(code)
            first = max(listed, lo) if listed else (min(have) if have else lo)
        missing = {d for d in returned if lo <= d <= hi and d not in have}
        absent = [day for day in self._dates(first, hi) if day not in have]
        if absent:
            span = calendar.Span(self.conn(), market_of(code), absent[0], absent[-1])
            missing |= {day for day in absent
                        if self._expected(span.is_trading_day(day), day, unknown_required=unknown_required)}
        return sorted(missing - set(exclude)), have

    def _day_covered(self, code, lo: str, hi: str, *, unknown_required=True, from_first_row=False,
                     returned=(), exclude=()) -> bool:
        """[lo, hi] 内每个应有日线的交易日都有可读的 final 行（口径见 _day_missing）。"""
        return not self._day_missing(code, lo, hi, unknown_required=unknown_required,
                                     from_first_row=from_first_row, returned=returned, exclude=exclude)[0]

    @staticmethod
    def _missing_ranges(missing, have) -> list:
        """缺的日子按连续区段合并：两个缺日之间没有可读行（只隔着非交易日）即同一段。"""
        present = sorted(have)
        ranges = []
        for day in missing:
            if ranges:
                i = bisect.bisect_right(present, ranges[-1][1])
                if i >= len(present) or present[i] > day:
                    ranges[-1][1] = day
                    continue
            ranges.append([day, day])
        return [tuple(r) for r in ranges]

    def _track_rejected_days(self, conn, gap, rows) -> set:
        """日线缺口续传中取回过、提交后仍无可读 final 行的日子逐日登记 rejected 缺口（严格口径），父缺口据其余日子
        判定覆盖；这样被拒的证据在下次返回空时也不会丢。rejected 与定稿缺口本身已是单日严格口径，不再拆分。"""
        if gap["dataset"] != "day" or gap["reason"] in ("rejected", "finalize"):
            return set()
        lo, hi = gap["start"][:10], gap["end"][:10]
        have = {r["trade_date"] for r in facts.read_day_rows(conn, gap["code"], lo, hi) if r["provenance"] == "final"}
        rejected = {r.trade_date for r in rows if lo <= r.trade_date <= hi} - have
        if rejected:
            with facts.write_txn(conn):
                for day in sorted(rejected):
                    # 同一天已有未解决的非严格缺口（含单日父缺口本身）时原地升级；record_gap 按范围去重不看原因
                    conn.execute("UPDATE coverage_gaps SET reason='rejected' WHERE code=? AND dataset='day'"
                                 " AND start=? AND end=? AND resolved_at IS NULL AND reason NOT IN"
                                 " ('rejected', 'finalize', 'known_gap')", (gap["code"], day, day))
                    facts.record_gap(conn, gap["code"], "day", day, day, "rejected")
        return rejected

    def _strict_children(self, gap) -> set:
        """父日线缺口区间内由其他单日严格缺口（rejected，或已转 known_gap 的单日缺口）承接的日子：父缺口的覆盖判定
        不再要求它们，避免父子重复取数。"""
        if gap["dataset"] != "day" or gap["reason"] in ("rejected", "finalize"):
            return set()
        lo, hi = gap["start"][:10], gap["end"][:10]
        return {g["start"][:10] for g in facts.open_gaps(self.conn(), gap["code"], "day")
                if g["gap_id"] != gap["gap_id"] and g["reason"] in ("rejected", "known_gap") and g["start"] == g["end"]
                and lo <= g["start"][:10] <= hi}

    def _gap_covered(self, gap, *, fetched: bool, rows=()) -> bool:
        """缺口是否已由可读事实覆盖。取数前按严格口径；定稿缺口始终严格（定稿只在交易日发生）；
        被拒缺口始终严格（上游返回过该日，续传返回空不能证明休市）；
        rows 为本次取回的行：取回过行的日子即使全部被拒也必须覆盖。"""
        strict = not fetched or gap["reason"] in ("finalize", "rejected")
        returned = {r.trade_date for r in rows}
        if gap["dataset"] == "day":
            exclude = self._strict_children(gap)
            return self._day_covered(gap["code"], gap["start"], gap["end"], unknown_required=strict,
                                     from_first_row=not strict, returned=returned - exclude, exclude=exclude)
        return self._minute_covered(gap["code"], gap["dataset"], gap["start"], gap["end"], unknown_required=strict,
                                    returned=returned)

    def _reconcile(self, conn, code, dates) -> None:
        """写入后核对分钟与日线（与定稿一致）：只核对同时有 final 日线与已收盘分钟的日期。调用方持写者锁。"""
        fact, dates = self._fact_freq(code), sorted(set(dates))
        if not fact or not dates:
            return
        finals = {r["trade_date"] for r in facts.read_day_rows(conn, code, dates[0], dates[-1])
                  if r["provenance"] == "final"}
        minutes = {r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM current_minute_bars WHERE code=? AND fact_freq=? AND state='closed'"
            " AND trade_date>=? AND trade_date<=?", (code, fact, dates[0], dates[-1]))}
        for day in dates:
            if day in finals and day in minutes:
                facts.reconcile_day(conn, code, day, market=market_of(code), fact_freq=fact)

    @staticmethod
    def _record_gap_safely(conn, code, dataset, start, end, reason) -> None:
        """写失败后的待补登记；库本身不可写时只能记日志（读侧继续返回之前的可信快照）。"""
        try:
            with facts.write_txn(conn):
                facts.record_gap(conn, code, dataset, start, end, reason)
        except (facts.FactsWriteError, sqlite3.Error):
            log.error("缺口登记失败 code=%s dataset=%s %s..%s", code, dataset, start, end, exc_info=True)

    def discover_missing(self, code, datasets=None) -> int:
        """首次规划之后：每轮历史追赶从发现水位到昨天（最近已收盘日，今天交给定稿）查新缺的交易日并登记缺口。

        水位 discovered:<dataset>:<code> 只在登记之后推进，停机期间缺的日子因此不会被之后的定稿「越过」。
        水位缺失时从首次规划日起算（首次整段规划覆盖到规划日前一天）。返回新登记的缺口数。"""
        conn = self.conn()
        last = (self.now().date() - timedelta(days=1)).isoformat()
        fact = self._fact_freq(code)
        registered = 0
        for dataset, marker in (("day", "backfill"), (fact, "minute")):
            if dataset is None or (datasets is not None and dataset not in datasets):
                continue
            key = f"discovered:{dataset}:{code}"
            since = facts.setting(conn, key)
            if since is None:
                planned = facts.setting(conn, self._plan_key(code, marker))
                if planned is None:
                    continue
                since = (date.fromisoformat(planned) - timedelta(days=1)).isoformat()
            if since >= last:
                continue
            lo = (date.fromisoformat(since) + timedelta(days=1)).isoformat()
            if dataset == "day":
                missing = self._day_missing(code, lo, last)[0]
                ranges = [(missing[0], missing[-1])] if missing else []
            else:
                missing = self._minute_uncovered_days(code, dataset, lo, last)
                ranges = ([(a, b) for a, b, *_ in self._month_slices(code, missing[0], missing[-1])]
                          if missing else [])
            with self.writer() as wconn, facts.write_txn(wconn):
                for start, end in ranges:
                    facts.record_gap(wconn, code, dataset, start, end, "backfill")
                facts.set_setting(wconn, key, last)
            registered += len(ranges)
        return registered

    def plan_minute_backfill(self, code) -> int:
        fact = self._fact_freq(code)
        if fact is None:
            return 0
        start = self._start(code, years=config.MINUTE_BACKFILL_YEARS)
        new = 0
        with self.writer() as conn, facts.write_txn(conn):
            before = len(facts.open_gaps(conn, code, fact))
            for lo_slot, hi_slot, lo, hi in self._month_slices(code, start, self._yesterday(code)):
                if not self._minute_covered(code, fact, lo.isoformat(), hi.isoformat()):
                    facts.record_gap(conn, code, fact, lo_slot, hi_slot, "backfill")
            new = len(facts.open_gaps(conn, code, fact)) - before
        return new

    def _fetch_gap(self, gap):
        code, dataset = gap["code"], gap["dataset"]
        market, kind = market_of(code), kind_of(code)
        if dataset == "day":
            source, gen = bindings.active(self.conn(), market, kind, FetchItem.DAY_HISTORY)
            rows = self._call(code, source, lambda p: p.day_history(code, gap["start"], gap["end"]),
                              recovery=gap["reason"] not in _NON_RECOVERY)
            return rows, source, gen
        source, gen = bindings.active(self.conn(), market, kind, FetchItem.MINUTE_HISTORY)
        rows = self._call(code, source, lambda p: p.minute_history(code, dataset, gap["start"], gap["end"],
                                                                   now=self.now()),
                          recovery=gap["reason"] not in _NON_RECOVERY, minute=True)
        return rows, source, gen

    def drain_gaps(self, max_requests, *, market=None, code=None, since=None, gaps=None, codes=None) -> dict:
        """按缺口续传（结束点从新到旧）。market 限定只处理该市场的缺口；codes 限定只处理这些代码（全库续传只给跟踪
        集合：移出自选或只查看过的代码留下的缺口保留但不再续传）；code 只处理该代码的缺口，
        since 只处理结束于该日及之后的缺口（请求路径追赶用）；gaps 给定时只处理这些缺口（历史追赶已排好队）。

        - 结束于今天或之后的缺口不在这里取：今天的分钟只来自盘中与定稿（计划 A 决定 8）；
        - 同一缺口至多一个线程在途（F24）：历史追赶线程、请求路径追赶各自调用本函数，先占用 gap_id 再取数，
          已被占用的跳过（不占请求数、不计尝试）；占用后按 gap_id 重读，别人刚关闭或转 known_gap 的也跳过；
        - 取数前已被可读事实覆盖的缺口直接关闭，不占请求；
        - 只有提交后该区间的可读覆盖达标（日线逐交易日、分钟逐槽位）才关闭；否则计一次尝试，
          全部被拒或槽位不全都算；满 MAX_GAP_ATTEMPTS 转 known_gap；
        - 标的级失败同样计次；连接类失败与写失败只记错误、不消耗次数；
        - 写入后对同时有 final 日线与已收盘分钟的日期做分钟与日线核对；港股定稿日线缺口补上后刷新供应商缓存。
        """
        counts = {"done": 0, "failed": 0, "incomplete": 0}
        exhausted = False
        today = self.now().date().isoformat()
        for gap in (facts.open_gaps(self.conn(), code) if gaps is None else gaps):
            if sum(counts.values()) >= max_requests:
                break
            if gap["reason"] == "known_gap" or (market and market_of(gap["code"]) != market) or not self._current_gap(gap):
                continue                               # 旧粒度缺口只读保留，不再续传
            if codes is not None and (gap["code"] not in codes or not self.eligible(gap["code"], gap["dataset"])):
                continue                               # 每条请求前复核：本轮中途移出自选的代码不再续传
            if gap["end"][:10] >= today or (since and gap["end"][:10] < since):
                continue
            with self._claim_gap(gap["gap_id"]) as mine:
                if not mine:
                    continue
                gap = self.conn().execute("SELECT * FROM coverage_gaps WHERE gap_id=?", (gap["gap_id"],)).fetchone()
                if gap is None or gap["resolved_at"] is not None or gap["reason"] == "known_gap":
                    continue
                try:
                    outcome = self._drain_one(gap)
                except CollectorLocked:
                    raise
                except Exception as exc:  # noqa: BLE001 — 取回后的处理出错：按一次失败计，免得每轮无限重取
                    log.warning("缺口续传异常，按一次失败计 code=%s gap=%s", gap["code"], gap["gap_id"], exc_info=True)
                    self._fail_gap_safely(gap["gap_id"], exc)
                    outcome = "failed"
            if outcome == "exhausted":
                exhausted = True
                break
            if outcome in counts:
                counts[outcome] += 1
        return {**counts, "budget_exhausted": exhausted}

    def _fail_gap_safely(self, gap_id, exc) -> None:
        """续传中途抛出非 provider 异常（核对、被拒日登记等出错）时记一次失败尝试（满 MAX_GAP_ATTEMPTS 转 known_gap）。
        异常也可能发生在取数之前，同样计一次：持续出错的缺口因此会停下，而不是每轮重试。库不可写时只记日志。"""
        try:
            with self.writer() as conn, facts.write_txn(conn):
                row = conn.execute("SELECT * FROM coverage_gaps WHERE gap_id=?", (gap_id,)).fetchone()
                if row is not None and row["resolved_at"] is None and row["reason"] != "known_gap":
                    self._fail_gap(conn, row, f"{type(exc).__name__}: {exc}")
        except (facts.FactsWriteError, sqlite3.Error, CollectorLocked):
            log.error("缺口失败记录写入失败 gap=%s", gap_id, exc_info=True)

    @contextmanager
    def _claim_gap(self, gap_id):
        """占用一条缺口（不等待）：已被别的线程占用时产出 False。"""
        with self._claims_guard:
            if gap_id in self._gap_claims:
                mine = False
            else:
                self._gap_claims.add(gap_id)
                mine = True
        try:
            yield mine
        finally:
            if mine:
                with self._claims_guard:
                    self._gap_claims.discard(gap_id)

    def _drain_one(self, gap) -> str:
        """续传一条已占用的缺口，返回 done / failed / incomplete / exhausted / skip（skip 不占请求数）。"""
        code, finalize = gap["code"], gap["reason"] == "finalize"
        if self._gap_covered(gap, fetched=False):
            with self.writer() as conn, facts.write_txn(conn):
                facts.resolve_gap(conn, gap["gap_id"])
            return "skip"
        try:
            rows, source, gen = self._fetch_gap(gap)
        except (_Skipped, ProviderUnsupported):
            return "skip"
        except _BudgetExhausted:
            return "exhausted"
        except ProviderError as exc:
            with self.writer() as conn, facts.write_txn(conn):
                if isinstance(exc, ProviderConnectionError):
                    conn.execute("UPDATE coverage_gaps SET last_error=? WHERE gap_id=?",
                                 (str(exc)[:500], gap["gap_id"]))
                else:
                    self._fail_gap(conn, gap, exc)
            return "failed"
        with self.writer() as conn:
            try:
                if gap["dataset"] == "day":
                    self._commit_day(conn, code, rows, item=FetchItem.DAY_HISTORY.value,
                                     source=source, gen=gen, start=gap["start"], end=gap["end"])
                    self._derive_calendar(conn, code)
                else:
                    self._commit_minutes(conn, code, rows, item=FetchItem.MINUTE_HISTORY.value,
                                         fact=gap["dataset"], source=source, gen=gen,
                                         start=gap["start"], end=gap["end"])
            except facts.StaleBinding:
                return "skip"
            except facts.FactsWriteError as exc:
                log.warning("缺口续传写入失败 code=%s dataset=%s", code, gap["dataset"], exc_info=True)
                try:
                    with facts.write_txn(conn):
                        conn.execute("UPDATE coverage_gaps SET last_error=? WHERE gap_id=?",
                                     (str(exc)[:500], gap["gap_id"]))
                except (facts.FactsWriteError, sqlite3.Error):
                    pass
                return "failed"
            self._reconcile(conn, code, [r.trade_date for r in rows])
            self._track_rejected_days(conn, gap, rows)
            gap = conn.execute("SELECT * FROM coverage_gaps WHERE gap_id=?", (gap["gap_id"],)).fetchone()
            covered = self._gap_covered(gap, fetched=True, rows=rows)
            with facts.write_txn(conn):
                if covered:
                    facts.resolve_gap(conn, gap["gap_id"])
                else:
                    self._fail_gap(conn, gap, "提交后可读覆盖未达标")
        if not covered:
            return "incomplete"
        if finalize and gap["dataset"] == "day" and market_of(code) == "HK" and kind_of(code) == "stock":
            self.refresh_vendor_qfq(code, self.now(), closed_through=gap["start"][:10], eligible=self.is_tracked)
        return "done"

    @staticmethod
    def _fail_gap(conn, gap, error) -> None:
        facts.fail_gap(conn, gap["gap_id"], error)
        if gap["attempts"] + 1 >= MAX_GAP_ATTEMPTS:
            conn.execute("UPDATE coverage_gaps SET reason='known_gap' WHERE gap_id=?", (gap["gap_id"],))

    def ensure_window(self, code, freq, bars=config.DEFAULT_WINDOW, *, wait=None) -> bool:
        """首次打开（视图为空或不足窗口时门面同步调用）：搜索查看的代码只补所请求周期的分析窗口，不登记缺口、
        不做规划（_ensure_view_window）；刚加入、还没整段回填的自选同样只补窗口，整段回填与分钟规划交历史线程；
        已回填的自选取够分钟窗口，其余月份登记缺口（spec §5.1）。
        同一代码的并发首取、后台规划与重拉只有一个在途，其余等待后读结果（spec D7）。
        wait 给定（门面的首开）时，本次调用里取各单飞锁（该代码与嵌套的上证指数）合计至多等这么多秒，等不到抛
        FlightBusy：历史线程的整段回填或港股缓存首建持锁时，首开不无限等待（目标 2026-09-29 第四阶段复核）。"""
        if not is_enabled():
            return False
        if wait is None:
            with self._single_flight(code):
                return self._ensure_window(code, freq, bars)
        prev = getattr(self._local, "flight_deadline", None)
        self._local.flight_deadline = time.monotonic() + wait
        try:
            with self._single_flight(code):
                return self._ensure_window(code, freq, bars)
        finally:
            self._local.flight_deadline = prev

    def _ensure_window(self, code, freq, bars) -> bool:
        market = market_of(code)
        conn = self.conn()
        if market == "CN":                          # 查看上证指数本身时同样先保证它的系统日线（单飞锁可重入）
            with self._single_flight(_CN_INDEX):    # 锁序固定：先标的、后指数；指数规划不再取其他标的的锁
                if not self._planned(_CN_INDEX, "backfill"):
                    self.backfill_day(_CN_INDEX)    # 否则前复权链第一步就是 unknown_calendar
        if not self.is_tracked(code) or not self._planned(code, "backfill"):
            # 搜索查看，或自选还没做过整段回填（刚加入就打开）：同步只补所请求周期的窗口，先把图给出来；
            # 自选的十年日线整段回填与分钟月度规划交历史线程（目标 2026-09-29：自选首开先服务近期窗口）
            return self._ensure_view_window(code, freq, bars)
        if freq in ("day", "week") or not self._fact_freq(code):
            self._ensure_vendor(code, eligible=self.is_tracked)
            return bool(facts.read_day_rows(conn, code)[:1])
        fact = self._fact_freq(code)
        target, base = sessions.FREQ_MINUTES.get(freq), sessions.FREQ_MINUTES[fact]
        if target is None or target < base:
            self._ensure_vendor(code, eligible=self.is_tracked)
            return bool(facts.read_day_rows(conn, code)[:1])
        need = bars * (target // base)
        start = self._start(code, years=config.MINUTE_BACKFILL_YEARS)
        source, gen = bindings.active(conn, market, kind_of(code), FetchItem.MINUTE_HISTORY)
        have = self._readable_minutes(code, fact)
        for n, (lo_slot, hi_slot, lo, hi) in enumerate(self._month_slices(code, start, self._yesterday(code))):
            if have >= need or n >= _MAX_WINDOW_SLICES:
                break
            if not self.is_tracked(code):
                return self._ensure_view_window(code, freq, bars)
            if self._minute_covered(code, fact, lo.isoformat(), hi.isoformat()):
                continue
            try:
                rows = self._call(code, source, lambda p: p.minute_history(code, fact, lo_slot, hi_slot,
                                                                           now=self.now()), minute=True)
            except (_Skipped, _BudgetExhausted, ProviderError):
                break
            with self.writer() as wconn:
                try:
                    self._commit_minutes(wconn, code, rows, item=FetchItem.MINUTE_HISTORY.value,
                                         fact=fact, source=source, gen=gen, start=lo_slot, end=hi_slot)
                except facts.StaleBinding:
                    break
                except facts.FactsWriteError:
                    log.warning("首取分钟写入失败，登记待补 code=%s %s..%s", code, lo_slot, hi_slot, exc_info=True)
                    self._record_gap_safely(wconn, code, fact, lo_slot, hi_slot, "write_failed")
                    break
                self._reconcile(wconn, code, [r.trade_date for r in rows])
            have = self._readable_minutes(code, fact)
        self._fetch_today_closed(code, self.now())     # 分钟窗口截到昨天：盘中首开顺手补今天已收盘槽
        if not self.is_tracked(code):
            return self._ensure_view_window(code, freq, bars)
        self.plan_minute_backfill(code)
        with self.writer() as wconn:
            self._mark_planned(wconn, code, "minute")
        self._ensure_vendor(code, window=need, widen=True, eligible=self.is_tracked)   # 分钟窗口之后建：缓存窗口按所请求周期折算的 m30 根数
        return have > 0

    # ---- 搜索查看的分析窗口（目标 2026-09-29 第二阶段）：非跟踪代码只维护最近窗口及必要依赖 ----
    def _ensure_view_window(self, code, freq, bars) -> bool:
        """非自选首开：只补所请求周期的最近窗口（_window_bounds），不做 10 年日线与三年分钟规划、不登记缺口；
        缺的部分下次打开或请求路径追赶按覆盖重新判断。港股按窗口建供应商缓存。"""
        self._sync_window(code, freq, bars)
        day_lo, minute_lo = self._window_bounds(code, freq, bars)
        if minute_lo is None:
            self._ensure_vendor(code)
            self._widen_vendor_day(code)
            return bool(facts.read_day_rows(self.conn(), code)[:1])
        fact = self._fact_freq(code)
        need = bars * (sessions.FREQ_MINUTES[freq] // sessions.FREQ_MINUTES[fact])
        self._ensure_vendor(code, window=need, widen=True)
        return self._readable_minutes(code, fact) > 0

    def _widen_vendor_day(self, code) -> None:
        """港股供应商日线缓存起点晚于 raw 日线起点（例如先开日线、再开周线补了更早的 raw）时整段重取一次，缓存随窗口
        向前扩；同一 raw 起点只试一次（供应商历史较短时不在每次打开时反复重取），一个请求都没发出不算试过。"""
        if market_of(code) != "HK" or kind_of(code) != "stock":
            return
        bars, meta = hk_vendor_qfq.read(self.conn(), code, "day")
        raw_first = self._raw_first(code, "day")
        if meta is None or not bars or not raw_first or raw_first >= bars[0]["dt"][:10] \
                or self._vendor_widened.get((code, "day")) == raw_first:
            return
        sent = []
        try:
            self.refresh_vendor_qfq(code, self.now(), on_sent=lambda: sent.append(1))
        finally:
            if sent:                                    # 发出过请求就算试过（发布出错照样）；一个都没发出不算
                self._vendor_widened[(code, "day")] = raw_first

    def _back_days(self, code, count, floor) -> str:
        """从昨天起按交易日（日历未知按工作日）回推 count 个交易日的日期，不早于 floor。
        日历按段取（每段够剩余交易日再加余量），不逐日查询。"""
        market, day, span = market_of(code), self.now().date() - timedelta(days=1), None
        while day.isoformat() > floor:
            iso = day.isoformat()
            if span is None or iso < span.start:
                lo = max(floor, (day - timedelta(days=max(count, 0) * 7 // 5 + 31)).isoformat())
                span = calendar.Span(self.conn(), market, lo, iso)
            if self._expected(span.is_trading_day(iso), iso, unknown_required=True):
                count -= 1
                if count <= 0:
                    break
            day -= timedelta(days=1)
        return max(day.isoformat(), floor)

    def _window_bounds(self, code, freq, bars, *, lookup=True) -> tuple:
        """(日线窗口起点, 分钟窗口起点或 None)。日线：周线按每周 5 个交易日折算，其余 bars 个交易日（分钟周期的前复权
        读同一日线窗口），截到 DAY_BACKFILL_YEARS；分钟：所请求周期折算的事实根数按每日槽数换成交易日，截到
        MINUTE_BACKFILL_YEARS 与首根日线（上市前没有分钟）。两者都另加 _WINDOW_MARGIN_DAYS 个交易日余量。"""
        day_floor = self._start(code, years=config.DAY_BACKFILL_YEARS, lookup=lookup)
        day_lo = self._back_days(code, (bars * 5 if freq == "week" else bars) + _WINDOW_MARGIN_DAYS, day_floor)
        fact, target = self._fact_freq(code), sessions.FREQ_MINUTES.get(freq)
        if not fact or target is None or target < sessions.FREQ_MINUTES[fact]:
            return day_lo, None
        need = bars * (target // sessions.FREQ_MINUTES[fact])
        days = -(-need // len(sessions.slots(market_of(code), fact))) + _WINDOW_MARGIN_DAYS
        first = next((r["trade_date"] for r in facts.read_day_rows(self.conn(), code)
                      if r["provenance"] == "final"), None)
        floor = max(self._start(code, years=config.MINUTE_BACKFILL_YEARS, lookup=lookup), first or "")
        return day_lo, self._back_days(code, days, floor)

    def _window_span(self, code, freqs, bars, *, lookup=True) -> tuple:
        """多个周期合并的 (日线窗口起点, 分钟窗口起点或 None)：各取最早（主图周期加共振依赖）。"""
        bounds = [self._window_bounds(code, f, bars, lookup=lookup) for f in ((freqs,) if isinstance(freqs, str) else freqs)]
        minutes = [m for _, m in bounds if m is not None]
        return min(d for d, _ in bounds), (min(minutes) if minutes else None)

    def _day_window_missing(self, code, lo, hi) -> list:
        """日线窗口 [lo, hi] 里缺可读 final 行的应有交易日（区段）。历史下限只认已证明的：上市日已知时从上市日起；
        否则只有曾经请求过（day_absent_from 水位）且上游没返回的首行之前的日子才算无数据，本地首行本身不是证明
        （先开日线、再开周线时，首行只是上一次窗口的起点）。"""
        listed = self._known_list_date(code)
        missing, have = self._day_missing(code, max(lo, listed) if listed else lo, hi)
        exempt = self._window_exempt(code, missing)
        if not listed and missing:
            asked = facts.setting(self.conn(), f"day_absent_from:{code}")
            first = self._raw_first(code, "day")
            prefix = [d for d in missing if asked and first and asked <= d < first]
            if prefix:                                    # 开市证据优先于「首行之前无数据」水位
                traded = self._traded_days(code, min(prefix), max(prefix))
                exempt |= {d for d in prefix if d not in traded}
        return self._missing_ranges([d for d in missing if d not in exempt], have)

    def _window_exempt(self, code, days) -> set:
        """days 中按无数据处理的日子：日历未知的工作日，本代码曾在一次成功的日线请求里请求过它而上游没返回
        （_note_window_absent），且现在仍没有开市证据（_traded_days）。窗口判断因此不再每次打开都判落后、重复请求
        （港股年表只取当年，过去年份的假日都是未知）；日历后来确认开市、或之后出现开市证据时不再豁免。
        开市证据按候选日子的范围一次收集（读取路径上不逐日查询）。"""
        absent = self._absent_days(code)
        if not absent:
            return set()
        conn, market = self.conn(), market_of(code)
        cand = [d for d in days if d in absent and calendar.is_trading_day(conn, market, d) is None]
        if not cand:
            return set()
        traded = self._traded_days(code, min(cand), max(cand))
        return {d for d in cand if d not in traded}

    def _traded_days(self, code, lo, hi) -> set:
        """[lo, hi] 内本代码有开市证据的日子：可读 final 日线（含停牌行）、已收盘分钟，港股个股另加已发布的
        供应商前复权缓存。日线漏返回时分钟或缓存仍能证明该日开市。"""
        lo, hi, conn = lo[:10], hi[:10], self.conn()
        days = {r["trade_date"] for r in facts.read_day_rows(conn, code, lo, hi) if r["provenance"] == "final"}
        days |= {r["trade_date"] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM current_minute_bars WHERE code=? AND state='closed'"
            " AND trade_date>=? AND trade_date<=?", (code, lo, hi))}
        if market_of(code) == "HK" and kind_of(code) == "stock":
            days |= {r["d"] for r in conn.execute(
                "SELECT DISTINCT substr(b.dt, 1, 10) AS d FROM vendor_qfq_bars b JOIN vendor_qfq_publish p"
                " ON b.code=p.code AND b.freq=p.freq AND b.version=p.version"
                " WHERE b.code=? AND substr(b.dt, 1, 10)>=? AND substr(b.dt, 1, 10)<=?", (code, lo, hi))}
        return days

    def _absent_days(self, code) -> set:
        raw = facts.setting(self.conn(), f"window_absent:{code}")
        return set(json.loads(raw)) if raw else set()

    def _note_window_absent(self, conn, code, lo, hi, rows) -> None:
        """一次成功的窗口日线请求 [lo, hi] 里，日历未知、上游没返回且本代码没有任何开市证据（_traded_days：final 日线、
        已收盘分钟、港股已发布缓存）的工作日记为本代码无数据（window_absent）。只记日历未知的日子：已知开市日没返回仍是缺，下次照常补；只记首根行之后的日子（首行之前由
        day_absent_from 承接，免得港股起点之前的整段工作日都记进来）。"""
        got = {r.trade_date for r in rows} | self._traded_days(code, lo, hi)
        if not got:
            return
        new = {d for d in self._dates(max(lo, min(got)), hi) if d not in got and date.fromisoformat(d).weekday() < 5
               and calendar.is_trading_day(conn, market_of(code), d) is None}
        old = self._absent_days(code)
        if new - old:
            with facts.write_txn(conn):
                facts.set_setting(conn, f"window_absent:{code}", json.dumps(sorted(old | new)))

    def _note_day_absent(self, conn, code, lo, hi, rows) -> None:
        """一次成功的日线请求 [lo, hi] 证明 lo 到首根可读行之前没有数据：有返回行时（从首个返回日起算），或空返回且区间
        整段在本地首行之前。记最早的请求起点（day_absent_from），窗口判断缺日时据此不重复请求上市前的日子。"""
        first = self._raw_first(code, "day")
        if not first or (not rows and hi >= first):
            return
        key = f"day_absent_from:{code}"
        old = facts.setting(conn, key)
        if old is None or lo < old:
            with facts.write_txn(conn):
                facts.set_setting(conn, key, lo)

    def _window_jobs(self, code, freqs, bars, *, force=False, datasets=("day", "minute"), lookup=True) -> list:
        """补窗口的请求清单 [(dataset, lo, hi)]，从新到旧：日线与分钟各按缺的交易日合并成连续区段（分钟再按自然月
        切），截到昨天；force（手动重拉）时整段重取。freqs 为一个或多个周期（取各自窗口的并集）。只读不写库；
        本地没有上市日时默认向上游查一次，lookup 为假时只用本地（lagging 零请求）。
        分钟窗口下限依赖首根日线：调用方先补日线、再算分钟（_sync_window 分两段）。"""
        last = (self.now().date() - timedelta(days=1)).isoformat()
        day_lo, minute_lo = self._window_span(code, freqs, bars, lookup=lookup)
        jobs = []
        if "day" in datasets and day_lo <= last:
            ranges = [(day_lo, last)] if force else self._day_window_missing(code, day_lo, last)
            jobs += [("day", lo, hi) for lo, hi in reversed(ranges)]
        if "minute" in datasets and minute_lo is not None and minute_lo <= last:
            fact, close = self._fact_freq(code), _CLOSE_SLOT[market_of(code)]
            if force:
                ranges = [(minute_lo, last)]
            else:
                days = self._minute_uncovered_days(code, fact, minute_lo, last)
                exempt = self._window_exempt(code, days)
                days = [d for d in days if d not in exempt]
                # 分段只被真有分钟行的日子隔开：周末、节假日、停牌日「无须分钟」也算覆盖，但不能把缺口切碎
                have = {r["trade_date"] for r in self.conn().execute(
                    "SELECT DISTINCT trade_date FROM current_minute_bars WHERE code=? AND fact_freq=?"
                    " AND state='closed' AND trade_date>=? AND trade_date<=?", (code, fact, minute_lo, last))}
                ranges = self._missing_ranges(days, have)
            for lo, hi in reversed(ranges):
                for slot_lo, slot_hi, _, _ in self._month_slices(code, lo, hi):
                    jobs.append((fact, slot_lo, f"{slot_hi[:10]} {close}"))
        return jobs

    def _expects_rows(self, code, dataset, lo, hi) -> bool:
        """[lo, hi] 里有应有返回行的日子：空返回因此不能算取到。日线是应有交易日（日历未知的工作日按应有，上市前除外）；
        分钟再除去停牌日（日线 sf=1）：整段或整月停牌时分钟空返回是合法的。"""
        market, listed = market_of(code), self._known_list_date(code)
        span = calendar.Span(self.conn(), market, lo, hi)
        days = [d for d in self._dates(lo, hi) if self._expected(span.is_trading_day(d), d, unknown_required=True)
                and not (listed and d < listed)]
        if dataset != "day" and days:
            suspended = {r["trade_date"] for r in facts.read_day_rows(self.conn(), code, lo[:10], hi[:10])
                         if r["sf"] == 1 and r["provenance"] != "preopen"}
            days = [d for d in days if d not in suspended]
        return bool(days)

    def _returned_covers(self, code, dataset, lo, hi, rows, res) -> bool:
        """强制重取的一个请求本次是否取全（不借旧库）：没有被拒的行；区间内每个应有交易日（上市前、已证明无数据的
        日子除外）都有返回的日线；分钟则这些日子（停牌日除外）的应有槽位都在本次返回里。应有交易日是日历已知的开市日，
        加上日历未知、但本代码库内已有开市证据（final 日线、已收盘分钟或港股缓存，_traded_days）的工作日（日历缺行
        时旧事实不能掩盖本次遗漏）；日历未知且没有任何证据的工作日无法确认，不要求返回（港股年表只取当年、raw 只推出
        开市日，过去年份的假日都属此类）。"""
        if res is not None and (getattr(res, "rejected", 0) or getattr(res, "pending_review", 0)):
            return False
        market, conn = market_of(code), self.conn()
        listed = self._known_list_date(code)
        asked, first = facts.setting(conn, f"day_absent_from:{code}"), self._raw_first(code, "day")
        proven = self._traded_days(code, lo, hi)
        days = [d for d in self._dates(lo, hi)
                if (calendar.is_trading_day(conn, market, d) or (d in proven and date.fromisoformat(d).weekday() < 5
                                                                 and calendar.is_trading_day(conn, market, d) is None))
                and not (listed and d < listed)
                and not (asked and first and asked <= d < first and d not in proven)]
        if dataset == "day":
            return set(days) <= {r.trade_date for r in rows}
        suspended = {r["trade_date"] for r in facts.read_day_rows(conn, code, lo[:10], hi[:10])
                     if r["sf"] == 1 and r["provenance"] != "preopen"}
        got = {r.slot_end for r in rows}
        return all(self._required_slots(market, dataset, d) <= got for d in days if d not in suspended)

    def _today_closed_gap(self, code, fact, now):
        """今天已收盘、越过槽值稳定余量、但库里还没有可读 closed 行的分钟槽位补取区间 (lo_slot, hi_slot)；
        盘前、休市、日历未知或已覆盖（含隔离键豁免）时 None，调用方据此零请求。分钟历史一律截到昨天
        （_window_jobs）、盘中增量只从当下往后追加，盘中首开或新加自选时今天已收盘的槽没有别的来源，
        最晚要等当晚定稿——首开、重拉、窗口追赶与盘中增量首轮在取数后顺手补上（2026-10-09 沃尔德盘中加入自选后早盘
        4 根 m15 缺失、当天 m30 缺两根，重拉也补不上）。返回 (lo_slot, hi_slot)；已覆盖（含隔离键豁免）时
        "covered"；休市、日历未知、盘前或无越过稳定余量的槽时 None（本时刻无可补，下轮重判）。"""
        market = market_of(code)
        today = now.date().isoformat()
        if calendar.is_trading_day(self.conn(), market, today) is not True:
            return None
        stable_at = now - timedelta(seconds=_TODAY_SLOT_SETTLE_S)
        if stable_at.date() != now.date():      # 午夜后第一分钟：余量跨回昨天，今天还没有任何槽收盘
            return None
        stable = stable_at.strftime("%H:%M")
        slots = sorted(s for s in self._required_slots(market, fact, today) if s[11:] <= stable)
        if not slots:
            return None
        conn = self.conn()
        hidden = facts.quarantined_keys(conn, code, fact)
        have = {r["slot_end"] for r in conn.execute(
            "SELECT slot_end FROM current_minute_bars WHERE code=? AND fact_freq=? AND state='closed'"
            " AND trade_date=?", (code, fact, today))}
        if set(slots) - hidden <= have:
            return "covered"
        return f"{today} 09:30", slots[-1]

    def _fetch_today_closed(self, code, now) -> str | None:
        """窗口取数或盘中增量首轮前补一次今天已收盘槽（分钟历史，至多一个请求）。

        返回 "ok"（取回行并提交）、"empty"（空返回：停牌日或上游盘中尚无当天，本交易日读取侧不再自动
        重试）、"covered"（已覆盖零请求）、"failed"（取数或写入失败，可下轮再试）、None（条件不满足：
        盘前、休市、日历未知、分钟停用）。失败只记日志不改窗口结果：自选由当晚定稿兜底、非自选下次
        打开按覆盖重判；写失败与盘中增量同款登记当日缺口。"""
        fact = self._fact_freq(code)
        if not fact:
            return None
        span = self._today_closed_gap(code, fact, now)
        if span is None:
            return None
        if span == "covered":
            return "covered"
        lo_slot, hi_slot = span
        market, kind = market_of(code), kind_of(code)
        source, gen = bindings.active(self.conn(), market, kind, FetchItem.MINUTE_HISTORY)
        try:
            rows = self._call(code, source, lambda p: p.minute_history(code, fact, lo_slot, hi_slot,
                                                                       now=now), minute=True)
        except (_Skipped, _BudgetExhausted, ProviderError):
            return "failed"
        with self.writer() as conn:
            try:
                self._commit_minutes(conn, code, rows, item=FetchItem.MINUTE_HISTORY.value, fact=fact,
                                     source=source, gen=gen, start=lo_slot, end=hi_slot, now=now)
            except facts.StaleBinding:
                return "failed"
            except facts.FactsWriteError:
                log.warning("当日已收盘槽补取写入失败 code=%s %s..%s", code, lo_slot, hi_slot, exc_info=True)
                self._record_gap_safely(conn, code, fact, lo_slot,
                                        f"{now.date().isoformat()} {_CLOSE_SLOT[market]}", "write_failed")
                return "failed"
            self._reconcile(conn, code, [r.trade_date for r in rows])
        return "ok" if rows else "empty"

    def _prefill_today(self, code, now) -> None:
        """盘中增量首轮前补今天已收盘槽：盘中加入自选或进入查看租期的代码，minute_live（最近两槽）覆盖不到
        此前已收盘的槽（2026-10-09 沃尔德 10:48 盘中加入自选后早盘 4 根 m15 缺失到当晚定稿）。每（代码，
        交易日）至多一次取数机会：补到、空返回或已覆盖都记 done；失败不记，下轮受退避与额度约束重试。"""
        today = now.date().isoformat()
        for stale in [d for d in self._today_prefilled if d != today]:
            del self._today_prefilled[stale]
        done = self._today_prefilled.setdefault(today, set())
        if code in done:
            return
        if self._fetch_today_closed(code, now) in ("ok", "empty", "covered"):
            done.add(code)

    def _sync_window(self, code, freqs, bars, *, force=False, max_requests=None) -> dict:
        """执行 _window_jobs：先日线、后分钟（分钟下限随补齐的日线重算）。上游或写入失败即停（不删旧数据、不登记缺口）；
        max_requests 用完也停。日线提交后推日历，写入后核对分钟与日线。

        返回 {requests, accepted, empty, short, stopped, complete}：accepted 是有行被接纳的请求数；empty 是应有交易日
        却空返回（或全被拒）的请求数；short 是强制重取时本次返回没取全的请求数（_returned_covers，不借旧库）；
        stopped 为停下的原因（fetch_failed、write_failed、stale_binding、max_requests）或 None；complete 为结束时
        窗口已无缺（按库内覆盖重新判断：说明现在可服务，不说明本次取到）。"""
        market, kind = market_of(code), kind_of(code)
        out = {"requests": 0, "accepted": 0, "empty": 0, "short": 0, "stopped": None, "complete": False}
        for phase in ("day", "minute"):
            for dataset, lo, hi in self._window_jobs(code, freqs, bars, force=force, datasets=(phase,)):
                if max_requests is not None and out["requests"] >= max_requests:
                    out["stopped"] = "max_requests"
                    break
                item = FetchItem.DAY_HISTORY if dataset == "day" else FetchItem.MINUTE_HISTORY
                source, gen = bindings.active(self.conn(), market, kind, item)
                out["requests"] += 1
                try:
                    if dataset == "day":
                        rows = self._call(code, source, lambda p: p.day_history(code, lo, hi))
                    else:
                        rows = self._call(code, source, lambda p: p.minute_history(code, dataset, lo, hi,
                                                                                   now=self.now()), minute=True)
                except (_Skipped, _BudgetExhausted, ProviderError):
                    out["stopped"] = "fetch_failed"
                    break
                with self.writer() as conn:
                    try:
                        if dataset == "day":
                            res = self._commit_day(conn, code, rows, item=item.value, source=source, gen=gen,
                                                   start=lo, end=hi)
                            self._derive_calendar(conn, code)
                            self._note_day_absent(conn, code, lo, hi, rows)
                            self._note_window_absent(conn, code, lo, hi, rows)
                        else:
                            res = self._commit_minutes(conn, code, rows, item=item.value, fact=dataset,
                                                       source=source, gen=gen, start=lo, end=hi)
                    except facts.StaleBinding:
                        out["stopped"] = "stale_binding"
                        break
                    except facts.FactsWriteError:
                        log.warning("窗口写入失败 code=%s %s %s..%s", code, dataset, lo, hi, exc_info=True)
                        out["stopped"] = "write_failed"
                        break
                    self._reconcile(conn, code, [r.trade_date for r in rows])
                if self._written(res):
                    out["accepted"] += 1
                elif self._expects_rows(code, dataset, lo, hi):
                    out["empty"] += 1
                if force and not self._returned_covers(code, dataset, lo, hi, rows, res):
                    out["short"] += 1
            if out["stopped"]:
                break
        out["complete"] = not self._window_jobs(code, freqs, bars)
        # 今天已收盘槽不在窗口内（截到昨天）：盘中首开/重拉/追赶顺手补；结果单列，不进 complete/stopped
        out["today_slots"] = self._fetch_today_closed(code, self.now())
        return out

    def refetching(self, code) -> bool:
        """同一代码的手动重拉是否在途（门面据此只读现有快照，不进入会等它的锁的首取与补窗口）。"""
        with self._catchup_guard:
            return code in self._refetching

    def refetch_window(self, code, freq=config.WINDOW_MINUTE_FREQ, bars=None) -> dict:
        """手动重拉（显式请求）：整段重取主图周期与共振依赖（WINDOW_MINUTE_FREQ）的分析窗口，不改变关注状态、
        不重复全库重建；失败时保留已有事实（提交走同一准入，修订不删行）。

        当天：盘中取一次盘中增量；过了首个定稿时点的交易日强制重取当天（与定稿同一入口与当日定稿锁，锁忙则跳过）。
        港股个股另整段重发供应商前复权缓存（窗口起点）。返回 {status, requests, accepted, empty, stopped, today,
        vendor}：status 为 ok（每个请求都取到并接纳、窗口无缺、当天与缓存都成功）、partial（有取到但不全）、
        failed（什么都没取到）、busy（同一代码已有重拉在途，在途追赶 _REFETCH_WAIT_CATCHUP_S 内没结束，或同一代码的
        单飞锁被历史规划、缓存刷新等占着超过 REQUEST_LOCK_WAIT_S：不发请求）或 disabled。"""
        if not is_enabled():
            return {"status": "disabled"}
        bars = bars or config.DEFAULT_WINDOW
        freqs = tuple(dict.fromkeys((freq, config.WINDOW_MINUTE_FREQ)))
        with self._catchup_guard:
            if code in self._refetching:                # 同一代码已有重拉在途：独占，后来者不重复整段取数
                return {"status": "busy"}
            self._refetching.add(code)
            running = self._catchups.get(code)
        try:
            # 已在途的追赶先做完，重拉再整段重取；预算内没结束就报忙，不与它并行请求
            if running is not None and not running.wait(_REFETCH_WAIT_CATCHUP_S):
                return {"status": "busy"}
            return self._refetch(code, freqs, bars)
        finally:
            with self._catchup_guard:
                self._refetching.discard(code)

    def _window_reviews(self, code, day_lo, minute_lo, hi) -> int:
        """窗口内待裁决的计数（调用方只看是否为零），按各自依赖的窗口分界：日线待核验（键为日期）按条查 [day_lo, hi]；
        分钟待核验（现行粒度，键为槽位）与现行粒度的分钟日线核对不一致按交易日查 [minute_lo, hi]（没有分钟窗口时不查）。
        hi 含今天。切换前的 m5 证据不计（facts.review_days）。"""
        conn = self.conn()
        n = conn.execute("SELECT COUNT(*) FROM pending_review WHERE code=? AND verdict IS NULL AND dataset='day'"
                         " AND key>=? AND key<=?", (code, day_lo, hi)).fetchone()[0]
        if minute_lo is not None:
            n += len({d for d in facts.review_days(conn, code, self._fact_freq(code), minute_lo) if d <= hi})
        return n

    def _refetch(self, code, freqs, bars) -> dict:
        now = self.now()
        stack = contextlib.ExitStack()
        try:
            stack.enter_context(self._bounded_flight(code, config.REQUEST_LOCK_WAIT_S))
        except FlightBusy:                              # 历史规划、缓存刷新等长任务持锁：不等它，不发请求
            return {"status": "busy"}
        with stack:
            out = self._sync_window(code, freqs, bars, force=True)
            out["today"] = self._refetch_today(code, now)
            if out.get("today_slots") == "failed" and out["today"] != "failed":
                # 今天已收盘槽的补取失败不能让重拉报 ok：盘中它补早盘（minute_live 只回最近两槽），
                # 收盘后到定稿前它是当天分钟的唯一来源（_refetch_today 此时不取）
                out["today"] = "failed"
            out["vendor"] = None
            if market_of(code) == "HK" and kind_of(code) == "stock":
                res = self.refresh_vendor_qfq(code, now, force=True)
                out["vendor"] = "ok" if isinstance(res, dict) and "stale" not in res.values() and \
                    not ({"stale_binding", "superseded", "frozen"} & set(res)) else "failed"
            day_lo, minute_lo = self._window_span(code, freqs, bars)   # 补齐日线后的窗口（分钟下限随首根日线）
            out["review"] = self._window_reviews(code, day_lo, minute_lo, now.date().isoformat())
        good = [out["accepted"] > 0, out["today"] in (None, "ok"), out["vendor"] in (None, "ok")]
        clean = (out["stopped"] is None and not out["empty"] and not out["short"] and out["complete"]
                 and not out["review"])
        if all(good) and clean:
            out["status"] = "ok"
        elif out["accepted"] or out["today"] == "ok" or out["vendor"] == "ok":
            out["status"] = "partial"
        else:
            out["status"] = "failed"
        return out

    def _refetch_today(self, code, now):
        """重拉的当天部分：None（今天不在交易或收盘到定稿之间：当天收盘数据只来自定稿）、ok、failed 或 busy。"""
        market = market_of(code)
        open_, known = self._finalize_open(market, now)
        today = now.date().isoformat()
        if open_:
            rec = self._final_record(code, today, manual=True)       # 显式请求：不计、不重置自动次数
            with self._try_flight(code, self._final_locks) as mine:
                if not mine:
                    return "busy"
                try:
                    outcome = self._finalize_code(code, today, now, rec, fetch=True, force=True)
                except CollectorLocked:
                    return "busy"
            forced = rec.pop("forced", None)             # 按本次取回的当天行判断，不借库内已完整的旧当天
            if outcome != "done" or not forced or not forced["day"] or forced["rejected"]:
                return "failed"
            fact = self._fact_freq(code)
            if forced["day"][-1].sf != 1 and fact and not self._required_slots(market, fact, today) <= forced["slots"]:
                return "failed"
            return "ok"
        if self._fact_freq(code) and self._trading(market, now) and self._in_session(now):
            # 按本次实际可读接纳判断：全拒、部分拒、待核验、落在隔离键上（计入 _written 却不可读）都不算成功；
            # forming 让位给已收盘槽（forming_over_closed：今天段或定格轮已写入 closed）是优先级保护而非拒收，
            # 全部因此被挡下时也视为可读接纳（该槽已有更权威的已收盘值）
            res, rows = self._intraday_one(code, now)
            if res is None:
                return "failed"
            hard = [r for r in res.rejected if r[1] != admission.FORMING_OVER_CLOSED]
            if hard or res.pending_review or (not self._written(res) and not res.rejected):
                return "failed"
            conn, fact, today = self.conn(), self._fact_freq(code), now.date().isoformat()
            slots = {r.slot_end for r in rows if r.trade_date == today}
            hidden = facts.quarantined_keys(conn, code, fact)
            if not slots or slots & hidden or today in facts.quarantined_keys(conn, code, "day"):
                return "failed"
            return "ok"
        return None

    def _readable_minutes(self, code, fact) -> int:
        """当前可读的已收盘分钟数（与视图同口径：排除隔离槽与隔离日）；历史 revision 与盘中 forming 不算。"""
        conn = self.conn()
        hidden, hidden_days = facts.quarantined_keys(conn, code, fact), facts.quarantined_keys(conn, code, "day")
        return sum(1 for r in conn.execute("SELECT slot_end, trade_date FROM current_minute_bars WHERE code=?"
                                           " AND fact_freq=? AND state='closed'", (code, fact))
                   if r["slot_end"] not in hidden and r["trade_date"] not in hidden_days)

    @staticmethod
    def _has_preopen_binding(code) -> bool:
        try:
            bindings.binding(market_of(code), kind_of(code), FetchItem.PREOPEN_REF)
        except KeyError:                             # 港股与指数没有 F4：前收即上一日收盘
            return False
        return True

    def preopen_confirmed(self, code, today) -> bool:
        """确认以事实库为准：当日 day 行可读且 pc 为有限正数（准入拒绝的盘前行不算）。"""
        rows = facts.read_day_rows(self.conn(), code, today, today)
        pc = rows[-1]["pc"] if rows else None
        return isinstance(pc, (int, float)) and math.isfinite(pc) and pc > 0

    def preopen(self, codes, now, *, active=None) -> set:
        """盘前参考价：返回提交后事实库里已确认（当日前收可读）的标的集合。active 给定时每个请求前复核（调度线程：
        查看租期中途到期或移出自选的代码不再开始请求）。"""
        today = now.date().isoformat()
        confirmed = set()
        for code in codes:
            if not self._has_preopen_binding(code) or (active is not None and not active(code)):
                continue
            if self.preopen_confirmed(code, today):
                confirmed.add(code)
                continue
            source, gen = bindings.active(self.conn(), market_of(code), kind_of(code), FetchItem.PREOPEN_REF)
            try:
                row = self._call(code, source, lambda p: p.preopen_ref(code, today), capability="preopen_ref")
            except (_Skipped, _BudgetExhausted, ProviderError):
                continue
            if row is None:
                continue
            with self.writer() as conn:
                try:
                    self._commit_day(conn, code, [row], item=FetchItem.PREOPEN_REF.value, source=source, gen=gen,
                                     now=now)
                except (facts.StaleBinding, facts.FactsWriteError):
                    log.warning("盘前参考价未入库 code=%s", code, exc_info=True)
                    continue
            if self.preopen_confirmed(code, today):
                confirmed.add(code)
        return confirmed

    def _preopen_due(self, state, codes, now) -> list:
        """未确认、且距上次尝试满 60 秒的 F4 标的（每代码节流，PREOPEN 与 INTRADAY 共用）。"""
        today = now.date().isoformat()
        tried = state.setdefault("preopen_tried", {})
        return [c for c in codes if self._has_preopen_binding(c) and not self.preopen_confirmed(c, today)
                and (c not in tried or (now - tried[c]).total_seconds() >= 60)]

    def _preopen_round(self, state, codes, now, active=None) -> list:
        due = self._preopen_due(state, codes, now)
        for code in due:
            state["preopen_tried"][code] = now
        self.preopen(due, now, active=active)
        return due

    @staticmethod
    def _intraday_codes(state, codes, now) -> list:
        """本轮盘中增量取哪些代码：个股每轮都取；指数距上次选中满 INTRADAY_INDEX_INTERVAL_S 才取（额度接近上限时
        同样加倍）。按选中计时，被退避或额度跳过的指数也等下一个间隔，不补取。上午、下午收盘后的定格轮（含补轮，
        见 _closing_round）不节流：接口在槽边界后约 20–30 秒内还会改刚结束的那根，节流会让指数停在收盘竞价前的值直到定稿。"""
        interval = config.INTRADAY_INDEX_INTERVAL_S
        if state.get("quota_ratio", 0.0) > config.QUOTA_SLOWDOWN_RATIO:
            interval *= 2
        last = state.setdefault("index_live_at", {})
        picked = []
        for code in codes:
            if kind_of(code) == "index":
                settling = _closing_window(now, market_of(code)) is not None
                if not settling and code in last and (now - last[code]).total_seconds() < interval:
                    continue
                last[code] = now
            picked.append(code)
        return picked

    def intraday_tick(self, codes, now, *, active=None) -> int:
        """盘中增量：逐代码取一次。active 给定时每个请求前复核（调度线程按当前时钟复核查看租期与自选，
        不按轮首快照）。取增量前先补今天已收盘槽（_prefill_today，每代码每日至多一次）：盘中新进入
        跟踪或查看租期的代码，minute_live 的最近两槽覆盖不到进入之前已收盘的部分。"""
        written = 0
        for code in codes:
            if active is not None and not active(code):
                continue
            self._prefill_today(code, now)
            written += self._written(self._intraday_one(code, now)[0])
        return written

    def _intraday_one(self, code, now) -> tuple:
        """一个代码取一次盘中增量并提交：返回 (提交结果, 本次返回的行)；没取到、空返回或没提交时结果为 None
        （空返回不算失败，也不登记缺口）。"""
        fact = self._fact_freq(code)
        if fact is None:
            return None, []
        source, gen = bindings.active(self.conn(), market_of(code), kind_of(code), FetchItem.MINUTE_LIVE)
        try:
            rows = self._call(code, source, lambda p: p.minute_live(code, fact, now=now), capability="minute_live", minute=True)
        except (_Skipped, _BudgetExhausted, ProviderError):
            return None, []
        if not rows:
            return None, []
        with self.writer() as conn:
            try:
                return self._commit_minutes(conn, code, rows, item=FetchItem.MINUTE_LIVE.value, fact=fact,
                                            source=source, gen=gen, now=now), rows
            except facts.StaleBinding:
                return None, rows
            except facts.FactsWriteError:
                # 当日分钟由定稿补齐；登记当日缺口，定稿失败或停机时次日按缺口续传
                day = now.date().isoformat()
                log.warning("盘中分钟写入失败 code=%s", code, exc_info=True)
                self._record_gap_safely(conn, code, fact, f"{day} 09:30",
                                        f"{day} {_CLOSE_SLOT[market_of(code)]}", "write_failed")
                return None, rows

    @staticmethod
    def _written(res) -> bool:
        """本批有被接受的行（新增、修订或与已存值相同，含隔离键）；被拒与待核验不算。只用来决定是否推日历，
        定稿成败以可读覆盖判定。"""
        return res is not None and res.inserted + res.revised + res.skipped > 0

    # ---- 当日定稿（计划 2026-09-29 D1–D3）：按（代码，交易日）调度，完成与否以事实库为准 ----
    def finalize_due(self, codes, trade_date, now, *, calendar_known, eligible=None, day_only=False) -> dict:
        """调度线程的定稿：交易日首个定稿时点起，活跃且当日未完成的代码都有资格（不再按市场锁存）。

        - 已完成或待核验（终态）的代码跳过；库内已完整的代码零请求收尾（核对、关闭缺口），重启后不重取；
        - 需要取数时按（代码，交易日）节流：首次立即，失败后按实际失败时刻隔 FINALIZE_RETRY_S，至多
          FINALIZE_MAX_ATTEMPTS 次自动尝试，次数与上次失败时刻持久在台账（重启不清零）；日历未知的日子另按
          定稿时点折算次数上限（_retry_wait）；
        - 单代码锁 try-acquire，锁忙本轮跳过；写者锁被占、退避、冷却、额度、绑定切换、能力不可用都是暂缓：这次
          一个上游请求都没发出才不计次，已发出的照算；
        - eligible 给定时逐代码复核资格（本轮中途移出自选的代码不再开始定稿）；day_only 只定稿日线（不在自选的
          上证指数：系统依赖只要当日 final 日线）。两种模式是同一（代码，交易日）的记录，共用次数、间隔与终态；
          模式是这次执行的参数，不写进共享记录（锁外改模式会改掉另一线程在途执行的判据），完成分开记：只定稿日线
          完成记 day_done，完整定稿完成才记 done。
        返回 {"done", "failed", "review", "deferred"}：本轮各结果的代码（跳过的不列）。"""
        out = {"done": [], "failed": [], "review": [], "deferred": []}
        with self._finals_guard:
            for key in [k for k in self._finals if k[1] < trade_date]:
                del self._finals[key]
        for code in codes:
            if eligible is not None and not eligible(code):
                continue
            rec = self._final_record(code, trade_date)
            if rec["done"] or rec["terminal"] or (day_only and rec["day_done"]):
                continue
            with self._try_flight(code) as got, self._try_flight(code, self._final_locks) as mine:
                if not (got and mine):
                    outcome = "deferred"        # 持锁方在定稿：它的记录（含在途预占）不由没拿到锁的一方改
                else:
                    outcome = self._finalize_step(code, trade_date, now, rec, calendar_known=calendar_known,
                                                  eligible=eligible, day_only=day_only)
            out[outcome].append(code)
        return out

    def _final_record(self, code, day, *, manual=False) -> dict:
        """（代码，交易日）的定稿记录。自动尝试的次数、上次失败时刻、原因与终态持久在 settings（重启不清零，
        _save_final）；暂缓保持期只在内存。manual（手动重拉）给一份不入表、不落盘的临时记录：不计自动次数、不重置。"""
        fresh = {"attempts": 0, "last_at": None, "reason": None, "deferred": None, "hold_until": None,
                 "done": False, "day_done": False, "terminal": False, "manual": manual}
        if manual:
            return fresh
        key = (code, day)             # 只定稿日线与完整定稿是同一（代码，交易日）：共用次数、间隔与终态
        unresolved = False
        with self._finals_guard:
            rec = self._finals.get(key)
            if rec is None:
                saved = facts.setting(self.conn(), self._ledger_key(code, day)) or {}
                fresh.update({k: saved[k] for k in _LEDGER_FIELDS if k in saved})
                rec = self._finals[key] = fresh
                if rec.get("pending"):
                    # 上一个进程预占后没记下结果（失败时刻写不进或进程中止）：不知道这次尝试何时结束，间隔按现在
                    # 发现它的时刻保守计（不早于实际失败时刻），次数照预占算
                    rec.update(pending=False, last_at=max(rec["last_at"] or 0, self.clock()))
                    unresolved = True
        if unresolved:
            self._save_final(rec, code, day)                 # 锁外写：写者锁不嵌在记录表锁里
        return rec

    @staticmethod
    def _ledger_key(code, day) -> str:
        return f"{_LEDGER}{day}:{code}"

    def _save_final(self, rec, code, day) -> bool:
        """把台账写进 settings（同一事务删掉更早交易日的台账）；返回是否写成。调用方多已持写者锁（可重入）。
        次数在请求前已由 _reserve 落盘（带 pending 标记），这里写的是失败时刻、原因、终态与退还，并清掉 pending：
        写不进时只留内存并告警；重启读到仍带 pending 的记录按发现时刻计间隔（_final_record），次数不会少记。"""
        if rec.get("manual"):
            return True
        value = {k: rec[k] for k in _LEDGER_FIELDS}
        if self._save_budget(self._ledger_key(code, day), value, _LEDGER, day):
            return True
        log.warning("定稿台账写入失败 code=%s day=%s（本次只记在内存）", code, day)
        return False

    def _reserve(self, rec, code, day) -> bool:
        """自动请求发出前先把这次尝试（次数加一、时刻、pending）写进持久台账；写不进就不发请求（返回 False），免得
        重启后次数归零、超过上限。同一次定稿只预占一次，预占归这次执行（持当日定稿锁的一方）；之后失败由 _fail 记
        失败时刻，完成时清 pending；暂缓只在这次一个上游请求都没发出时由 _defer 退还（_sent）。手动重拉不预占。"""
        if rec.get("manual") or "_reserved" in rec:
            return True
        prev = (rec["attempts"], rec["last_at"])
        rec.update(attempts=prev[0] + 1, last_at=self.clock(), pending=True)
        if self._save_final(rec, code, day):
            rec["_reserved"], rec["_sent"] = prev, False
            return True
        rec.update(attempts=prev[0], last_at=prev[1], pending=False)
        return False

    def _finalize_step(self, code, day, now, rec, *, calendar_known, eligible=None, day_only=False) -> str:
        """调度线程与请求路径追赶共用的逐代码入口（调用方持该代码的当日定稿锁）：节流允许才取数。"""
        # 日线已成功的暂停态不属于失败重试：恢复后立即补分钟，当次失败仍用原台账节流。
        if rec.get("reason") == "minutes_paused" and self.minute_enabled(code):
            rec["reason"] = None
            wait = None
        else:
            wait = self._retry_wait(rec, now, market_of(code), calendar_known)
        try:
            return self._finalize_code(code, day, now, rec, fetch=wait is None, wait=wait, eligible=eligible,
                                       day_only=day_only)
        except CollectorLocked:
            return self._defer(rec, code, day, "writer_locked")

    @staticmethod
    def _retry_wait(rec, now, market, calendar_known):
        """本轮不许取数的原因（None 为可取）：暂缓保持期、自动次数用完（exhausted）、间隔未到（throttle），或日历未知
        且已过的定稿时点数已用完。"""
        ts = now.timestamp()
        if rec["hold_until"] is not None and ts < rec["hold_until"]:
            return rec["deferred"] or "throttle"
        if rec["attempts"] == 0:
            return None
        if rec["attempts"] >= config.FINALIZE_MAX_ATTEMPTS:
            return "exhausted"
        if ts - rec["last_at"] < config.FINALIZE_RETRY_S:
            return "throttle"
        if not calendar_known:
            hhmm = now.strftime("%H:%M")
            if rec["attempts"] >= sum(hhmm >= slot for slot in _FINALIZE_SLOTS[market]):
                return "calendar_unknown"
        return None

    def _final_status(self, code, day, *, checks=True, day_only=False) -> str:
        """当日定稿完成判据的只读部分（不写库、不联网）：complete、pending_review、reconcile_mismatch、quarantined、
        missing_final、missing_slots。

        不用视图的 _finalized（只查有行、无 forming），也不用本次提交结果（重启后就没了）。final 日线排除隔离；
        分钟按 _minute_covered 覆盖全部槽位（隔离槽不要求，不重取），但有应有槽位被隔离时判 quarantined（待核验终态，
        不算完成，与图表状态栏一致）；停牌豁免要求可读 final 且 sf=1，空返回不算停牌；
        未裁决的待核验（日线键为日期，分钟键为槽位）与最近一次核对不一致（checks）判待核验。
        没有核对记录（facts._checks_scope）不能区分「一致」与「没核对过」：核对放在写者锁内的 _settle。day_only 不看
        分钟，也不看分钟日线核对（系统依赖的分钟只按分析窗口维护）。"""
        conn, fact = self.conn(), None if day_only else self._fact_freq(code)
        unsettled = facts.day_unsettled(conn, code, day, fact)       # 与视图状态栏共用；只认现行分钟粒度
        if unsettled == "pending_review" or (checks and unsettled):
            return unsettled
        final = [r for r in facts.read_day_rows(conn, code, day, day) if r["provenance"] == "final"]
        if not final:
            return "missing_final"
        if final[-1]["sf"] != 1 and fact and not self._minute_covered(code, fact, day, day):
            return "missing_slots"
        if final[-1]["sf"] != 1 and fact and self._quarantined_slots(code, fact, day):
            # 应有槽位被隔离（已证实错误）：不重取（重取得到的仍是同一冲突），也不能认证完成——与图表状态栏同一判据
            return "quarantined"
        return "complete"

    def _quarantined_slots(self, code, fact, day) -> set:
        return self._required_slots(market_of(code), fact, day) & facts.quarantined_keys(self.conn(), code, fact)

    def _settle(self, conn, code, day, *, day_only=False) -> str:
        """写者锁内的完成判定：只读判据满足后核对分钟与日线（reconcile_day 会写库，所以不放进只读判据）。"""
        status = self._final_status(code, day, checks=False, day_only=day_only)
        fact = None if day_only else self._fact_freq(code)
        if status == "complete" and fact and facts.reconcile_day(conn, code, day, market=market_of(code),
                                                                 fact_freq=fact):
            return "reconcile_mismatch"
        return status

    def _finalize_code(self, code, day, now, rec, **kw) -> str:
        """_finalize_attempt 的外壳：任何异常退出（写者锁被占、发布或标 stale 写失败、数据库错误）都先了结本次预占
        （_release：发出过请求就照算，没发出才退还），再上抛；预占不残留到下一次尝试。"""
        try:
            return self._finalize_attempt(code, day, now, rec, **kw)
        except BaseException:
            try:
                self._release(rec, code, day)
            except Exception:  # noqa: BLE001 — 了结失败不覆盖原异常（台账仍是在途，重启按发现时刻保守计）
                log.warning("定稿预占了结失败 code=%s day=%s", code, day, exc_info=True)
            raise

    def _finalize_attempt(self, code, day, now, rec, *, fetch, wait=None, force=False,
                          eligible=None, day_only=False) -> str:
        """一个代码一次定稿：done / failed / review / deferred。目标日 day 与准入的 today 都取自任务开始时，
        跨过午夜才返回也提交给原日期。上游请求在写者锁外，锁内只做提交、核对与缺口。
        day_only 只取日线（这次执行的参数，调用方持定稿锁）；force（手动重拉）库内已完整也重取当天，并把本次取回的当天行记进 rec["forced"]
        （重拉按本次返回判断成败）。eligible 给定时在每个请求之前复核（日线与分钟之间、港股缓存刷新之前与其两个
        周期之间）：请求期间被移出的代码不再开始下一个请求，已取的照常提交，本次记暂缓（untracked），不登记缺口。"""
        if not fetch:
            # 节流中不做任何判定与写库（每 5 秒一轮：核对会写 day_checks 与 soft_flags）；节流期间变完整只可能来自
            # 请求路径追赶，它与调度线程共用 rec，完成时已记 done
            return "deferred" if wait == "throttle" else self._defer(rec, code, day, wait)
        market, kind = market_of(code), kind_of(code)
        if code == _CN_INDEX and self._planned(code, "backfill"):
            # 先登记停机期间缺的指数日线（零请求）：否则当天 final 一写入，日历推导就会把中间缺的日子当成休市
            self.discover_missing(code, datasets=("day",))
        fact = None if day_only else self._fact_freq(code)
        lo_slot, hi_slot = f"{day} 09:30", f"{day} {_CLOSE_SLOT[market]}"
        status = self._final_status(code, day, day_only=day_only)
        with self.writer():
            pass                      # 写者锁被其他写者占用时先暂缓，不白花请求（CollectorLocked 由调用方处理）
        failure = None
        dropped = False
        day_res = min_res = None
        if force or status in _NEED_FETCH:
            day_src, day_gen = bindings.active(self.conn(), market, kind, FetchItem.DAY_HISTORY)
            min_src, min_gen = bindings.active(self.conn(), market, kind, FetchItem.MINUTE_HISTORY)
            day_rows = minute_rows = None
            if not self._reserve(rec, code, day):
                return self._defer(rec, code, day, "ledger_unwritable")
            try:
                # 缺日线时停牌与否未知，分钟一并取；只缺分钟时不重取日线
                if force or status == "missing_final":
                    day_rows = self._sent_call(rec, code, day_src, lambda p: p.day_history(code, day, day))
                if fact and eligible is not None and not eligible(code):
                    dropped = True
                elif fact:
                    try:
                        minute_rows = self._sent_call(rec, code, min_src,
                                                      lambda p: p.minute_history(code, fact, lo_slot, hi_slot, now=now),
                                                      minute=True)
                    except _Skipped:
                        if self.minute_enabled(code):
                            raise
                        fact = None             # 日线请求途中取消了分钟：已取回的日线照常提交，按只日线完成
            except _Skipped:
                why = "backoff" if not self.code_backoff.allow(code) else "cooldown"
                return self._defer(rec, code, day, why, now=now, gap=True, fact=fact)
            except _BudgetExhausted:
                return self._defer(rec, code, day, "quota", now=now, gap=True, fact=fact)
            except ProviderUnsupported:
                # 能力不可用（provider 没注入、未验证）：重试不会变好但也没花请求——暂缓保持，发出过请求的照算
                return self._defer(rec, code, day, "unsupported", now=now, hold=True)
            except ProviderError as exc:
                failure = f"fetch:{type(exc).__name__}"          # 类名即脱敏类别，不带上游消息
            if failure is None:
                with self.writer() as conn:
                    try:
                        if day_rows is not None:
                            day_res = self._commit_day(conn, code, day_rows, item=FetchItem.DAY_HISTORY.value,
                                                       source=day_src, gen=day_gen, start=day, end=day, now=now)
                        if minute_rows is not None:
                            min_res = self._commit_minutes(conn, code, minute_rows,
                                                           item=FetchItem.MINUTE_HISTORY.value, fact=fact,
                                                           source=min_src, gen=min_gen, start=lo_slot, end=hi_slot,
                                                           now=now)
                    except facts.StaleBinding:
                        return self._defer(rec, code, day, "stale_binding")
                    except facts.FactsWriteError:
                        failure = "commit:write_failed"
                    if self._written(day_res) and any(r["provenance"] == "final"
                                                      for r in facts.read_day_rows(conn, code, day, day)):
                        self._derive_calendar(conn, code)
                if force:
                    rec["forced"] = {"day": [r for r in day_rows or () if r.trade_date == day],
                                     "slots": {r.slot_end for r in minute_rows or () if r.trade_date == day},
                                     "rejected": bool((day_res and day_res.rejected) or (min_res and min_res.rejected))}
            if dropped:
                return self._defer(rec, code, day, "untracked")
        with self.writer() as conn:
            if failure is None:
                status = self._settle(conn, code, day, day_only=day_only)
                if status in _NEED_FETCH:
                    res = day_res if status == "missing_final" else min_res
                    failure = f"check:{'admission_rejected' if res is not None and res.rejected else status}"
                elif status in _REVIEW:
                    failure = f"check:{status}"
            if failure is None:
                self._close_day_gaps(conn, code, day)
            else:
                # 日线与分钟各登记一条：之后只补回分钟时，缺 final 日线仍然可见
                self._record_finalize_gaps(conn, code, day, fact, f"finalize/{failure}")
                # 同一写者锁内计次（台账写入不另开锁窗口）
                return self._fail(rec, code, day, now, failure, terminal=failure in {f"check:{r}" for r in _REVIEW})
        if (market == "HK" and kind == "stock" and not day_only
                and not self._vendor_caught_up(code, day)):
            if eligible is not None and not eligible(code):     # 分钟请求期间被移出：raw 已提交，不再开始缓存请求
                return self._defer(rec, code, day, "untracked")
            if not self._reserve(rec, code, day):
                return self._defer(rec, code, day, "ledger_unwritable")
            res = self.refresh_vendor_qfq(code, now, closed_through=day, eligible=eligible, today_ok=True,
                                          on_sent=lambda: self._mark_sent(rec))
            if "untracked" in res:
                return self._defer(rec, code, day, "untracked")
            if not self._vendor_caught_up(code, day):
                # 退避、冷却、额度、绑定切换、冷备冻结、被更新版本取代：暂缓；本次一个请求都没发出才不计次
                why = res.get("deferred") or next((k for k in _VENDOR_DEFER if res.get(k)), None)
                if why:
                    return self._defer(rec, code, day, f"vendor:{why}", now=now, hold=True)
                return self._fail(rec, code, day, now, "vendor:not_published")    # 只重试缓存，不登记 raw 缺口
        if not self.minute_enabled(code):
            rec["reason"] = "minutes_paused"
        if rec.pop("_reserved", None) is not None:
            rec.pop("_sent", None)
            rec["pending"] = False
            self._save_final(rec, code, day)            # 清 pending：重启不把已完成的这次当成结果未知
        rec.update(day_done=True, deferred=None, hold_until=None)
        if not day_only and self.minute_enabled(code):
            rec["done"] = True                          # 只定稿日线完成不等于完整完成
        return "done"

    def _sent_call(self, rec, code, source, fn, *, minute=False):
        """定稿的上游请求：进入实际调用即记 rec["_sent"]（_call 的 on_sent：返回、报错与中断都算），此后的暂缓与
        异常都不退还预占。_Skipped、_BudgetExhausted 与 ProviderUnsupported 在发出前抛出（或按未发出退还），不记。"""
        return self._call(code, source, fn, recovery=True, on_sent=lambda: self._mark_sent(rec), minute=minute)

    @staticmethod
    def _mark_sent(rec) -> None:
        if "_sent" in rec:
            rec["_sent"] = True

    def _release(self, rec, code, day) -> None:
        """了结本次预占（没有预占时什么也不做）：发出过请求就照算一次、间隔从现在起；一个都没发出才退还次数与时刻。
        清掉在途标记并写台账（写不进时磁盘仍是在途，重启按发现时刻保守计）。"""
        prev, sent = rec.pop("_reserved", None), rec.pop("_sent", False)
        if prev is None:
            return
        if sent:
            rec.update(last_at=self.clock(), pending=False)
        else:
            rec.update(attempts=prev[0], last_at=prev[1], pending=False)
        self._save_final(rec, code, day)

    def _fail(self, rec, code, day, now, reason, *, terminal=False) -> str:
        """一次尝试未完成：计次、按失败时的时钟记时并写进持久台账（手动重拉的临时记录不写）；第 FINALIZE_MAX_ATTEMPTS 次失败后
        当日不再自动尝试（遗留缺口交次日历史追赶）。原因变化或次数用完时记一次 WARNING。"""
        if rec.pop("_reserved", None) is None:          # 请求前已预占的不再加一（没发请求就失败的，如核对不一致，这里计）
            rec["attempts"] += 1
        rec.pop("_sent", None)
        rec["last_at"] = self.clock()          # 实际失败时刻（慢请求、多代码串行时晚于本轮开始的 now）
        rec["pending"] = False
        exhausted = not rec.get("manual") and rec["attempts"] >= config.FINALIZE_MAX_ATTEMPTS
        rec.update(deferred=None, hold_until=None, terminal=terminal or exhausted)
        if reason != rec["reason"] or exhausted:
            log.warning("定稿未完成 code=%s day=%s 原因=finalize/%s（第 %d 次）%s", code, day, reason,
                        rec["attempts"], "，待核验：当日不再自动重试" if terminal
                        else "，自动尝试已用完：遗留缺口交次日历史追赶" if exhausted else "")
            rec["reason"] = reason
        self._save_final(rec, code, day)
        return "review" if terminal else "failed"

    def _defer(self, rec, code, day, why, *, now=None, gap=False, hold=False, fact=None) -> str:
        """暂缓：这次一个上游请求都没发出才不计次（_release 退还预占），已发出的照算；原因变化时记一次 INFO。
        退避、冷却、额度（gap）登记当日缺口（不写 last_error；fact 为分钟粒度，只定稿日线为 None），并保持
        FINALIZE_DEFER_S 不再看，避免每 5 秒一轮反复扣额度、写库；hold 只保持不登记（港股 raw 已齐、只差供应商
        缓存时不登记 raw 缺口）。"""
        self._release(rec, code, day)                    # 发出过请求照算，一个都没发出才退还
        if gap or hold:
            rec["hold_until"] = now.timestamp() + config.FINALIZE_DEFER_S
        if why != rec["deferred"]:
            log.info("定稿暂缓 code=%s day=%s 原因=%s", code, day, why)
            if gap:
                try:
                    with self.writer() as conn:
                        self._record_finalize_gaps(conn, code, day, fact, None)
                except CollectorLocked:
                    pass
        rec["deferred"] = why
        return "deferred"

    def _record_finalize_gaps(self, conn, code, day, fact, error) -> None:
        """登记当日定稿缺口（日线与分钟各一条）；error 给出时写进该日范围内全部未解决缺口的 last_error。"""
        self._record_gap_safely(conn, code, "day", day, day, "finalize")
        if fact:
            self._record_gap_safely(conn, code, fact, f"{day} 09:30", f"{day} {_CLOSE_SLOT[market_of(code)]}",
                                    "finalize")
        if error:
            try:
                with facts.write_txn(conn):
                    conn.execute("UPDATE coverage_gaps SET last_error=? WHERE code=? AND dataset IN (?, ?)"
                                 " AND resolved_at IS NULL AND substr(start, 1, 10)=? AND substr(end, 1, 10)=?",
                                 (error[:500], code, "day", fact or "day", day, day))
            except (facts.FactsWriteError, sqlite3.Error):
                log.error("定稿原因写入失败 code=%s %s", code, day, exc_info=True)

    def _close_day_gaps(self, conn, code, day) -> int:
        """当日已完成：该代码该日范围内的未解决缺口逐条按可读覆盖复核后关闭（零请求；冷却、额度耗尽时照样执行）。
        不按 reason 一刀切：record_gap 按范围去重，同一范围可能是 write_failed。复核在写事务内进行。"""
        fact, closed = self._fact_freq(code), 0
        try:
            with facts.write_txn(conn):
                for gap in facts.open_gaps(conn, code):
                    if (gap["dataset"] in ("day", fact) and gap["start"][:10] == day == gap["end"][:10]
                            and self._gap_covered(gap, fetched=False)):
                        facts.resolve_gap(conn, gap["gap_id"])
                        closed += 1
        except (facts.FactsWriteError, sqlite3.Error):
            log.error("当日缺口关闭失败 code=%s %s", code, day, exc_info=True)
            return 0
        return closed

    def _vendor_freqs(self, code):
        return hk_vendor_qfq.FREQS if self._fact_freq(code) else ("day",)

    def _vendor_caught_up(self, code, day) -> bool:
        """港股供应商前复权缓存已追到 day：两个周期都已发布、未冻结、未标 stale，且 day（含）之前没有比缓存末端
        更新的已收盘 raw（与视图的 lagging 同口径）；只日线实例只要求 day 缓存。"""
        conn = self.conn()
        for freq in self._vendor_freqs(code):
            bars, meta = hk_vendor_qfq.read(conn, code, freq)
            if meta is None or meta["frozen"] or meta["stale"]:
                return False
            last = bars[-1]["dt"] if bars else ""
            if freq == "day":
                newer = [r for r in facts.read_day_rows(conn, code, None, day)          # 停牌行不进缓存，同视图
                         if r["provenance"] == "final" and r["sf"] == 0 and r["trade_date"] > last[:10]]
            else:
                newer = [r for r in facts.read_minute_rows(conn, code, freq, last or None, f"{day} 23:59")
                         if r["state"] == "closed" and r["slot_end"] > last]
            if newer:
                return False
        return True

    _VENDOR_ITEMS = (FetchItem.DAY_HISTORY, FetchItem.MINUTE_HISTORY)

    def _raw_first(self, code, freq):
        """raw 事实的可读起点（排除隔离）：day 取 final 日线，m30 取已收盘分钟（盘中 forming 不算，缓存只收已收盘 bar）。"""
        conn = self.conn()
        hidden_days = facts.quarantined_keys(conn, code, "day")
        if freq == "day":
            return next((r["trade_date"] for r in facts.read_day_rows(conn, code) if r["provenance"] == "final"),
                        None)
        hidden = facts.quarantined_keys(conn, code, freq) | hidden_days
        for r in conn.execute("SELECT slot_end, trade_date FROM current_minute_bars WHERE code=? AND fact_freq=?"
                              " AND state='closed' ORDER BY slot_end", (code, freq)):
            if r["slot_end"] not in hidden and r["trade_date"] not in hidden_days:
                return r["trade_date"]
        return None

    def _window_start(self, code, through, bars) -> str:
        """m30 窗口起点：从 through 按交易日（日历未知按工作日）回推够 bars 根 m30，另加余量；
        不早于分钟回填目标起点（上市日、首根日线）。"""
        market = market_of(code)
        need = -(-bars // len(sessions.slots(market, "m30"))) + _WINDOW_MARGIN_DAYS
        floor = self._start(code, years=config.MINUTE_BACKFILL_YEARS)
        day = date.fromisoformat(through)
        while day.isoformat() > floor:
            if self._expected_day(market, day.isoformat(), unknown_required=True):
                need -= 1
                if need == 0:
                    break
            day -= timedelta(days=1)
        return max(day.isoformat(), floor)

    def _vendor_start(self, code, freq, raw_first, *, through, cached_first=None, window=config.DEFAULT_WINDOW):
        """缓存覆盖起点。day 跟随 raw 起点，已整段回填的自选不晚于回填目标起点（搜索查看与还没回填的自选不借缓存扩到回填目标）。m30 首建（cached_first 为空）只建默认窗口：
        raw 起点晚于窗口时跟随 raw，先开日线与先开 30 分因此一致，更早的历史由历史追赶排空缺口后整段扩展；
        已有缓存时跟随 raw 起点，没有 raw 时取窗口，整段重取不缩短已有覆盖。window 为窗口的 m30 根数。"""
        if freq == "day":
            if not self.is_tracked(code) or not self._planned(code, "backfill"):
                # 搜索查看只维护分析窗口：日线缓存跟随 raw（窗口）起点，不借缓存拉到回填目标；已有更早覆盖不缩短。
                # 刚加入、还没整段回填的自选同样先建窗口（首开不等深历史），回填后由历史线程扩展到回填目标
                floor = raw_first or self._window_bounds(code, "day", config.DEFAULT_WINDOW)[0]
                return min(floor, cached_first) if cached_first else floor
            target = self._start(code, years=config.DAY_BACKFILL_YEARS)
            return min(raw_first, target) if raw_first else target
        start = self._window_start(code, through, window)
        if cached_first is None:
            return max(raw_first, start) if raw_first else start
        return min(raw_first or start, cached_first)

    def refresh_vendor_qfq(self, code, now, *, closed_through=None, window=config.DEFAULT_WINDOW,
                           widen=False, force=False, eligible=None, today_ok=False, on_sent=None) -> dict:
        """港股供应商前复权缓存（spec §7 方案 (b) 规则 2、6）。

        - 以下成套周期在只日线实例中仅为 day，停用的 m30 缓存保留但不参与请求、发布与完成判断；
        - 首建、定稿、缺口补齐与扩展的刷新都走同一代码的单飞锁，同时只有一个在途；
        - 两个周期的候选都在写者锁外取齐，再由 hk_vendor_qfq.publish_set 在同一写事务里复核取数时的绑定代次
          与取数前的缓存版本后一起发布；版本已变（期间有更新版本发布）则丢弃迟到候选、不标 stale；
          任一周期取数失败或不合法，两个都不发布并标 stale；
        - 无缓存、冻结中、raw 覆盖起点早于缓存起点、或近 30 天比对出重述时，两个周期都整段重取；
          否则各自追加新收盘 bar；
        - widen：两个周期整段重取，m30 起点不晚于 window 根 m30 的窗口起点（首开按所请求周期补窗口）；
        - force（手动重拉）：两个周期整段重取，起点同常规；
        - 发布前核对候选覆盖（_vendor_covers，日线按交易日、m30 按槽位，隔离的键不要求）：整段替换时已发布版本在区间内
          的 bar 一根不少、raw 在候选首根之后的交易日与槽位都有，追加时 raw 在缓存末端之后的都有；不满足不发布，保留当前
          版本并标 stale（供应商历史比 raw 短是允许的，中间、尾部缺与缺槽不允许）；截止日不早于已发布缓存末端；
        - eligible 给定时在每个供应商请求前复核后台资格：移出自选后不再开始下一个请求，已取的候选不发布
          （两个周期只能一起发布），返回 {"untracked": True}；没有重述证据时不标 stale，已比对出重述的照标；
        - 历史类绑定在冷备期间冻结且不调用供应商；切回主源后保持冻结，直到两个周期都重取成功才随发布解冻；
        - 当天（now 的日期）的缓存只由定稿（today_ok，次数记在当日台账）与手动重拉（force）请求。其他自动刷新（首建、
          续传后刷新、扩展、打开页面补窗口）的请求一律只到最近一个已知开市日（_yesterday，日历没有记录时是自然日的
          昨天），不看缓存状态——覆盖当天的自动请求因此只来自台账记过的尝试。缓存已有当天尾部（当天定稿已发布）时：
          整段替换保留已发布的当天尾部（publish_set keep_tail，新版本沿用原 stale，不认证没重取的尾部），追加模式
          没有可追加的就不发布，冻结中不请求（尾部截断时解冻只由覆盖当天的定稿与手动重拉做），失败不标 stale（只取
          历史区间，失败不否定已服务的数据）——但任一周期取回后已比对出价格重述（重叠 bar 价格不同；没有重叠只说明
          要整段重取，不算）之后再失败或移出自选，照标 stale；异常退出（写者锁被占、请求中断等）同样按取数时的
          版本结算，写者锁被占时记进待补表，锁释放后（历史线程每轮、下次缓存刷新前）补落盘，期间有新版本发布则作废；
        - on_sent 给定时，每个进入实际调用的供应商请求（返回、上游报错或中断）都调用一次（_call 的 on_sent），调用方
          据此区分「没发出的暂缓」与「花了请求」（发布阶段再出异常也不丢）；ProviderUnsupported 返回
          {"deferred": "unsupported"}。
        """
        self._flush_vendor_stale()
        evidence = {}
        with self._single_flight(code):
            try:
                return self._refresh_vendor_qfq(code, now, closed_through=closed_through, window=window,
                                                widen=widen, force=force, eligible=eligible, today_ok=today_ok,
                                                on_sent=on_sent, evidence=evidence)
            except BaseException:
                # 任何异常退出（写者锁被占、请求中断、数据库错误）：已证实重述且还没结算的，按取数时的版本标 stale；
                # 写不进就留待锁释放后补落盘。原异常照抛
                if evidence.get("restated") and not evidence.get("settled"):
                    self._settle_restated(code, evidence["versions"])
                raise

    def _settle_restated(self, code, versions) -> None:
        """重述证据落盘（标 stale，只针对取数时的版本）；写者锁被占或写失败时记进待补表，不抛。"""
        try:
            with self.writer() as wconn:
                hk_vendor_qfq.mark_stale_if(wconn, code, versions)
            with self._pending_guard:                  # 只删自己处理的这一项：期间登记的更新版本证据保留
                if self._vendor_stale_pending.get(code) == versions:
                    del self._vendor_stale_pending[code]
        except Exception as exc:  # noqa: BLE001 — 结算失败不覆盖原异常；之后由 _flush_vendor_stale 补
            with self._pending_guard:                  # 不用较旧的证据覆盖较新的
                current = self._vendor_stale_pending.get(code)
                if current is None or _version_key(versions) >= _version_key(current):
                    self._vendor_stale_pending[code] = versions
            log.warning("重述证据暂未落盘（%s），稍后补 code=%s",
                        "写者锁被占" if isinstance(exc, CollectorLocked) else type(exc).__name__, code,
                        exc_info=not isinstance(exc, CollectorLocked))

    def _flush_vendor_stale(self) -> None:
        """补落盘待补的重述证据（历史线程每轮与每次缓存刷新前）；期间已有新版本发布的证据作废。"""
        with self._pending_guard:
            pending = list(self._vendor_stale_pending.items())
        for code, versions in pending:
            self._settle_restated(code, versions)

    def _refresh_vendor_qfq(self, code, now, *, closed_through, window, widen, force=False, eligible=None,
                            today_ok=False, on_sent=None, evidence=None) -> dict:
        conn = self.conn()
        # 本轮候选与发布集合固定；分钟发出前仍实时复核，途中启用留到下一轮。
        freqs = self._vendor_freqs(code)
        items = self._VENDOR_ITEMS if len(freqs) > 1 else (FetchItem.DAY_HISTORY,)
        states = {item: bindings.active(conn, "HK", "stock", item) for item in items}
        primary = bindings.binding("HK", "stock", FetchItem.DAY_HISTORY).primary
        if any(src != bindings.binding("HK", "stock", item).primary for item, (src, _) in states.items()):
            with self.writer() as wconn:
                hk_vendor_qfq.freeze(wconn, code)
            return {"frozen": True}
        gens = {item.value: gen for item, (_, gen) in states.items()}
        through = closed_through or self._vendor_through(code)
        cache = {freq: hk_vendor_qfq.read(conn, code, freq) for freq in freqs}
        # 截止日不早于已发布缓存末端（缓存里都是已收盘 bar）：续传旧缺口按缺口日刷新时，整段替换不丢末端
        ends = [bars[-1]["dt"][:10] for bars, _ in cache.values() if bars]
        through = max([through, *ends])
        today = now.date().isoformat()
        if through >= today and not (force or today_ok):              # 当天只归定稿与手动重拉：只取到昨天
            through = min(through, self._yesterday(code))
        tail = any(end > through for end in ends)          # 已发布的当天尾部：这次不请求，整段替换时原样保留
        versions = {freq: meta["version"] if meta else None for freq, (_, meta) in cache.items()}
        frozen = any(meta and meta["frozen"] for _, meta in cache.values())
        if frozen and tail:                                # 解冻要重新认证当天尾部：只由定稿与手动重拉（覆盖当天）做
            return {"frozen": True}
        raw_first = {freq: self._raw_first(code, freq) for freq in freqs}
        starts = {freq: self._vendor_start(code, freq, raw_first[freq], through=through, window=window,
                                           cached_first=bars[0]["dt"][:10] if bars else None)
                  for freq, (bars, _) in cache.items()}
        # raw 覆盖向前扩展（补历史之后）：缓存起点晚于 raw 起点即整段重取
        short = any(bars and raw_first[freq] and bars[0]["dt"][:10] > raw_first[freq]
                    for freq, (bars, _) in cache.items())
        if widen and "m30" in starts:
            starts["m30"] = min(starts["m30"], self._window_start(code, through, window))
        full = force or widen or frozen or short or any(meta is None for _, meta in cache.values())
        restated = set()                   # 已取回、与已发布版本比对出价格重述的周期：旧历史被否定，之后失败必须标 stale
        refetch = set()                    # 追加模式下需要整段重取的周期（重述或与缓存没有重叠）
        evidence = {} if evidence is None else evidence
        evidence.update(restated=restated, versions=versions, settled=False)   # 异常退出由 refresh_vendor_qfq 结算

        def failed():
            """所有显式的未发布退出共用：保留当天尾部且没有重述证据时不标 stale，否则标（结算后记 settled）。"""
            self._vendor_stale(code, cache, tail=tail and not restated)
            evidence["settled"] = True
        recent = (date.fromisoformat(through) - timedelta(days=_VENDOR_RECHECK_DAYS)).isoformat()

        def fetch(freq, lo):
            if eligible is not None and not eligible(code):
                raise _Ineligible(code)
            span = (lo, through) if freq == "day" else (f"{lo} 09:30", f"{through} 16:00")
            rows = self._call(code, primary, lambda p: p.qfq_series(code, freq, *span), recovery=True,
                              capability="qfq_series", on_sent=on_sent, minute=freq != "day")
            if cache[freq][1] is not None:
                # 每个周期一取回就比对：另一周期随后失败或移出自选时证据不丢（整段路径同样）
                if hk_vendor_qfq.price_conflict(conn, code, freq, rows, closed_through=through):
                    restated.add(freq)
                if hk_vendor_qfq.needs_refetch(conn, code, freq, rows, closed_through=through):
                    refetch.add(freq)
            return rows

        try:
            if not full:
                spans = {freq: max(starts[freq], recent) for freq in freqs}
                fresh = {freq: fetch(freq, spans[freq]) for freq in freqs}
                full = bool(refetch)
                if not full and tail:                      # 缓存已越过截止日、没有重述：没有可追加的
                    return {freq: None for freq in freqs}
            if full:
                spans = dict(starts)
                fresh = {freq: fetch(freq, spans[freq]) for freq in freqs}
        except _Ineligible:
            log.info("移出自选，停止供应商前复权刷新 code=%s", code)
            if restated:                                   # 已证实重述：停止请求与发布，但旧历史已被否定
                failed()
            return {"untracked": True}
        except (_Skipped, _BudgetExhausted, ProviderError) as exc:
            failed()
            out = {freq: "stale" for freq in freqs}
            if isinstance(exc, _BudgetExhausted):              # 请求没发出：调用方（定稿）据此暂缓、不计次
                out["deferred"] = "quota"
            elif isinstance(exc, ProviderUnsupported):
                out["deferred"] = "unsupported"
            elif isinstance(exc, _Skipped):
                out["deferred"] = "backoff" if not self.code_backoff.allow(self._backoff_key(code)) else "cooldown"
            return out
        short = [freq for freq in freqs
                 if not self._vendor_covers(code, freq, fresh[freq], spans[freq], through, cache[freq][0], full=full)]
        if short:
            log.warning("供应商前复权候选没有覆盖请求区间，保留旧版 code=%s freqs=%s", code, short)
            failed()
            return {freq: "stale" for freq in freqs}
        with self.writer() as wconn:          # 取锁或发布出异常：由 refresh_vendor_qfq 按取数时的版本结算证据
            try:
                return hk_vendor_qfq.publish_set(wconn, code, fresh, closed_through=through, full=full,
                                                 binding_gens=gens, unfreeze=frozen, expected_versions=versions,
                                                 keep_tail=tail, freqs=freqs)
            except facts.StaleBinding:
                log.info("绑定已切换，丢弃迟到的供应商前复权候选 code=%s", code)
                return {"stale_binding": True}
            except hk_vendor_qfq.VendorSuperseded:
                log.info("取数期间已有更新版本发布，丢弃迟到的供应商前复权候选 code=%s", code)
                return {"superseded": True}
            except (ValueError, hk_vendor_qfq.VendorFrozen):
                log.warning("供应商前复权候选不合法或缓存已冻结，保留旧版 code=%s", code, exc_info=True)
        failed()
        return {freq: "stale" for freq in freqs}

    def _vendor_covers(self, code, freq, rows, lo, through, cached, *, full) -> bool:
        """供应商候选是否覆盖它要发布的区间（只看已收盘的 bar；日线按交易日、m30 按槽位，两边标签同一归一口径），
        日线不要求停牌日：
        - full（整段替换）：已发布版本在 [lo, through] 内的时间标签都在候选里（截短的候选不能替换可服务的缓存）；
          raw 在候选首根之后、区间内的日线交易日或已收盘 m30 槽位候选也都有（供应商历史可以比 raw 短，中间与尾部不能缺）；
        - 追加：只追加缓存末端之后的 bar，raw 在缓存末端之后、through 之前的日线交易日或 m30 槽位候选都要有（不留断档、
          不缺槽）。"""
        rows = [b for b in rows if b["trade_date"] <= through]
        key = "trade_date" if freq == "day" else "slot_end"
        got = {b[key] for b in rows}
        if full:
            if not rows or not {b["dt"] for b in cached if lo <= b["dt"][:10] <= through} <= got:
                return False
            after = min(got)                               # 候选首根之后（含）：供应商历史可以比 raw 短
            since = max(lo, after[:10])
        else:
            after = cached[-1]["dt"] if cached else lo     # 缓存末端之后（不含）
            since = after[:10]
        conn = self.conn()
        hidden_days = facts.quarantined_keys(conn, code, "day")      # 与缓存读取同一隔离口径：隔离的键不要求
        if freq == "day":
            raw = {r["trade_date"] for r in facts.read_day_rows(conn, code, since, through)
                   if r["provenance"] == "final" and r["sf"] != 1 and r["trade_date"] not in hidden_days}
        else:
            hidden = facts.quarantined_keys(conn, code, freq)
            raw = {r["slot_end"] for r in conn.execute(
                "SELECT slot_end, trade_date FROM current_minute_bars WHERE code=? AND fact_freq=? AND state='closed'"
                " AND trade_date>=? AND trade_date<=?", (code, freq, since, through))
                if r["slot_end"] not in hidden and r["trade_date"] not in hidden_days}
        need = {k for k in raw if (k >= after if full else k > after)}
        return need <= got

    def _vendor_through(self, code) -> str:
        """未显式给截止日时的缓存截止日：最新可读 final 日线日期（不晚于今天）与昨天中较晚者，
        当天定稿后的补窗口与扩展刷新因此不撤掉当天已发布的尾部。"""
        today = self.now().date().isoformat()
        finals = [r["trade_date"] for r in facts.read_day_rows(self.conn(), code, None, today)
                  if r["provenance"] == "final"]
        return max([self._yesterday(code), *finals[-1:]])

    def _vendor_stale(self, code, cache, *, tail=False) -> None:
        """刷新失败标 stale。tail（缓存已越过这次的截止日，即当天定稿已发布的尾部，且这次没发现历史重述）时不标：
        这次只取历史区间（补窗口、扩展），失败不否定已服务的数据，当天也不再有自动请求替它清掉 stale；已发现重述
        （任一周期取回后比对出重述，调用方传 tail=False）时旧历史已被否定，照标。
        只标取数时的版本（mark_stale_if）：期间另一个实例已发布的新版本不因这次失败降级。"""
        if not tail and any(meta is not None for _, meta in cache.values()):
            with self.writer() as wconn:
                hk_vendor_qfq.mark_stale_if(wconn, code, {freq: meta["version"] if meta else None
                                                          for freq, (_, meta) in cache.items()})

    def _extend_vendor(self, code, now) -> bool:
        """历史追赶（非盘中）把自选的供应商缓存扩到 raw 起点，m30 与日线各自判断，任一需要就整段重取一次：

        - m30：全库续传排空分钟缺口后（known_gap 除外），raw m30 起点早于缓存 m30 起点；
        - 日线：日线缺口排空后（known_gap 除外），raw 日线起点早于缓存日线起点——首开只建窗口、随后日线整段回填完成，
          而分钟历史无从前扩时，也要把日线缓存扩到回填目标（目标 2026-09-29 第四阶段复核）。
        缺口未排空时不触发，免得每轮续传都整段重取；同一 raw 起点只试一次（日线与打开页面的 _widen_vendor_day 共用
        记录）：供应商历史比 raw 短或重取失败时，不在每 5 秒一轮的历史追赶里反复重取（失败的重试交给定稿时的整段重取
        判定）；一个请求都没发出（额度、退避、冷却）不算试过。返回是否触发。"""
        if market_of(code) != "HK" or kind_of(code) != "stock" or not self.is_tracked(code):
            return False
        conn = self.conn()
        marks = []
        for freq, dataset, key in (("m30", self._fact_freq(code), code), ("day", "day", (code, "day"))):
            if dataset is None:
                continue
            bars, _ = hk_vendor_qfq.read(conn, code, freq)
            raw_first = self._raw_first(code, freq)
            tried = (self._vendor_extended if freq == "m30" else self._vendor_widened).get(key)
            if not bars or not raw_first or raw_first >= bars[0]["dt"][:10] or tried == raw_first:
                continue
            if any(g["reason"] != "known_gap" for g in facts.open_gaps(conn, code, dataset)):
                continue
            marks.append((freq, key, raw_first))
        if not marks:
            return False
        sent = []
        try:
            self.refresh_vendor_qfq(code, now, eligible=self.is_tracked, on_sent=lambda: sent.append(1))
        finally:                                        # 发出过请求就算试过（发布出错照样）；一个都没发出下一轮再试
            if sent:
                for freq, key, raw_first in marks:
                    (self._vendor_extended if freq == "m30" else self._vendor_widened)[key] = raw_first
        return True

    def _ensure_vendor(self, code, window=config.DEFAULT_WINDOW, *, widen=False, eligible=None) -> None:
        """港股默认前复权视图读供应商缓存：没有缓存时首建；window 为窗口的 m30 根数（按所请求周期折算）。

        widen（首开按分钟周期补窗口）：已有缓存的可读 m30 少于 window、且窗口起点早于缓存起点时，
        以该窗口整段重取（例如先开日线只建了 520 根，再开 60 分要 1040 根）；同一窗口起点只试一次，
        供应商历史不够时不在每次打开时反复重取；一个请求都没发出（额度、退避、冷却）不算试过。"""
        if market_of(code) != "HK" or kind_of(code) != "stock":
            return
        conn = self.conn()
        # 只日线期间只建了日线缓存：重新勾选分钟后 m30 缺席也按首建处理
        if any(hk_vendor_qfq.read(conn, code, freq)[1] is None for freq in self._vendor_freqs(code)):
            self.refresh_vendor_qfq(code, self.now(), window=window, eligible=eligible)
            return
        if not self._fact_freq(code):
            return
        bars, meta = hk_vendor_qfq.read(conn, code, "m30")
        if not widen or meta is None or len(bars) >= window:
            return
        start = self._window_start(code, self._vendor_through(code), window)
        tried = self._vendor_widened.get(code)          # 已试过的最早起点：更晚起点的窗口已被它覆盖（m60、m30 交替打开）
        if (bars and start >= bars[0]["dt"][:10]) or (tried is not None and tried <= start):
            return
        sent = []
        try:
            self.refresh_vendor_qfq(code, self.now(), window=window, widen=True, eligible=eligible,
                                    on_sent=lambda: sent.append(1))
        finally:
            if sent:                                    # 发出过请求就算试过（发布出错照样）；一个都没发出（额度、退避、冷却、移出）不算
                self._vendor_widened[code] = start

    def refresh_calendar(self, market, now) -> None:
        source, _ = bindings.active(self.conn(), market, "any", FetchItem.SESSION_CALENDAR)
        years = {now.year} | ({now.year + 1} if now.month == 12 else set())
        for year in sorted(years):
            try:
                rows = self._call(f"calendar-{market}", source, lambda p: p.calendar(year),
                                  capability="calendar")
            except (_Skipped, _BudgetExhausted, ProviderError):
                continue
            with self.writer() as conn, facts.write_txn(conn):
                calendar.store_rows(conn, rows, source=source)
        with self.writer() as conn, facts.write_txn(conn):
            (calendar.derive_cn_past if market == "CN" else calendar.derive_hk_past)(conn)

    # ---- 查看集合与调度 ----
    def touch_viewing(self, code) -> None:
        self._viewing[code] = self.clock()

    def tracked_codes(self) -> list:
        """跟踪集合：自选（关注状态的唯一事实）加 A 股系统依赖上证指数（它的 final 日线推导过去交易日历，前复权相邻
        判断依赖它；系统角色不进入自选、不给其他代码开全拉的口子）。只有跟踪代码有历史规划、缺口续传、调度线程定稿与
        供应商缓存扩展；搜索查看的代码只维护分析窗口。"""
        return sorted(set(self.watchlist_fn()) | {_CN_INDEX})

    def is_tracked(self, code) -> bool:
        """用户跟踪（自选）：完整历史、分钟规划、调度定稿与供应商缓存扩展的资格。"""
        return code in set(self.watchlist_fn())

    def eligible(self, code, dataset="day") -> bool:
        """后台历史工作的资格，在每次请求前复核（不按轮快照）：自选全部数据集；不在自选的上证指数只有日线
        （系统依赖：日历推导与前复权相邻判断），它的分钟与其他搜索查看的代码一样只按分析窗口取。"""
        if dataset != "day" and not self.minute_enabled(code):
            return False
        return self.is_tracked(code) or (code == _CN_INDEX and dataset == "day")

    def active_codes(self, now) -> list:
        """盘前与盘中取数对象：自选加页面仍在看的代码（VIEWING_WINDOW_S 内有过图表请求；页面每 60 秒刷新一次，
        离开或隐藏页面后很快停止）。上证指数只有被自选或查看时才盘中取数。"""
        ts = now.timestamp()
        # 快照再遍历：请求线程随时 touch_viewing，调度与历史追赶两个线程都会读
        viewing = [c for c, t in dict(self._viewing).items() if ts - t <= config.VIEWING_WINDOW_S]
        return sorted(set(self.watchlist_fn()) | set(viewing))

    def _still_active(self, code) -> bool:
        """单个请求开始前按当前时钟复核取数资格：在自选，或最后一次查看距今不超过 VIEWING_WINDOW_S。
        active_codes 只是轮首快照；一轮里前面的请求耗时会让排在后面的代码越过租期。"""
        if code in set(self.watchlist_fn()):
            return True
        seen = self._viewing.get(code)
        return seen is not None and self.clock() - seen <= config.VIEWING_WINDOW_S

    def _trading(self, market, now) -> bool:
        known = calendar.is_trading_day(self.conn(), market, now.date().isoformat())
        return known if known is not None else now.weekday() < 5

    def _in_session(self, now) -> bool:
        """任一市场处于盘中取数窗口（含收盘后 1 分钟与港股竞价后 10 分钟）。"""
        hms = now.strftime("%H:%M:%S")
        windows = {"CN": (("09:30:00", "11:31:00"), ("13:00:00", "15:01:00")),
                   "HK": (("09:30:00", "12:01:00"), ("13:00:00", "16:11:00"))}
        return any(self._trading(m, now) and any(a <= hms <= b for a, b in w) for m, w in windows.items())

    # ---- 请求路径限时追赶（计划 2026-09-29 D4）----
    def _finalize_open(self, market, now) -> tuple:
        """(今天是否已到定稿时段, 日历是否已知)：交易日（日历未知按工作日）且过了首个定稿时点。"""
        known = calendar.is_trading_day(self.conn(), market, now.date().isoformat())
        trading = known if known is not None else now.weekday() < 5
        return bool(trading) and now.strftime("%H:%M") >= _FINALIZE_SLOTS[market][0], known is not None

    def lagging(self, code, now) -> bool:
        """只读（不联网、不写库）：本代码现在有没有可做的追赶。目标日是「最近一个应完成交易日」：过了首个定稿时点的
        交易日是今天（今天未完成且重试节流允许），否则只看到昨天为止（发现水位落后于昨天，或有结束于近
        CATCHUP_LOOKBACK_DAYS 天、今天之前的未解决缺口）；非跟踪代码（搜索查看）看分析窗口到昨天是否缺日线或分钟。
        数据已是最新时返回 False，调用方因此零请求。"""
        market, kind, fact = market_of(code), kind_of(code), self._fact_freq(code)
        today = now.date().isoformat()
        open_, known = self._finalize_open(market, now)
        rec = self._final_record(code, today) if open_ else None      # 含持久台账：重启后不绕过间隔与上限
        if open_ and not (rec and (rec["done"] or rec["terminal"])):
            rec = rec or {"attempts": 0, "last_at": None, "deferred": None, "hold_until": None}
            if self._retry_wait(rec, now, market, known) is None:
                status = self._final_status(code, today)
                if status in _NEED_FETCH or (status == "complete" and market == "HK" and kind == "stock"
                                             and not self._vendor_caught_up(code, today)):
                    return True
        if not self.is_tracked(code):
            return bool(self._window_jobs(code, config.WINDOW_MINUTE_FREQ, config.DEFAULT_WINDOW, lookup=False))
        if not self._planned(code, "backfill"):
            return False
        conn = self.conn()
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        for dataset, marker in (("day", "backfill"), (fact, "minute")):
            planned = facts.setting(conn, self._plan_key(code, marker)) if dataset else None
            if planned is None:
                continue
            since = (facts.setting(conn, f"discovered:{dataset}:{code}")
                     or (date.fromisoformat(planned) - timedelta(days=1)).isoformat())
            if since < yesterday:
                return True
        lo = (now.date() - timedelta(days=config.CATCHUP_LOOKBACK_DAYS)).isoformat()
        return any(g["reason"] != "known_gap" and lo <= g["end"][:10] < today and self._current_gap(g)
                   for g in facts.open_gaps(conn, code))

    def catch_up(self, code, now) -> dict:
        """追赶执行体（后台线程里同步运行）：先用 D1 的逐代码入口补今天（同一重试节流与当日定稿锁，调度线程正在定稿
        就跳过，不重复扣费），再发现并续传本代码近期缺的日子（只到昨天，从新到旧，至多 CATCHUP_MAX_REQUESTS 个请求，
        与历史追赶的续传一样不持单飞锁：同一代码的补窗口与首取不必等它；同一缺口与历史线程按 gap_id 互斥）；
        港股个股续传有进展时刷新前复权缓存。非跟踪代码不做发现与续传，只按 _sync_window 补分析窗口（同一请求上限，
        持单飞锁：与首取、手动重拉不并行请求）。请求上限只管历史部分：今天的定稿（日线与分钟各一次）与港股缓存刷新另计。
        目标日在开始时固定。"""
        market, today = market_of(code), now.date().isoformat()
        out = {}
        if code in self._refetching:                   # 手动重拉在途：它已覆盖窗口与当天，这里不再并行请求
            return {"skipped": "refetching"}
        open_, known = self._finalize_open(market, now)
        if open_:
            rec = self._final_record(code, today)
            with self._try_flight(code, self._final_locks) as mine:
                if mine and not (rec["done"] or rec["terminal"]):
                    out["finalize"] = self._finalize_step(code, today, now, rec, calendar_known=known)
        if not self.is_tracked(code):                  # 搜索查看：只补分析窗口（缺多少补多少，从新到旧、有请求上限）
            with self._single_flight(code):            # 与首取、手动重拉互斥：在途的那个补完后这里按覆盖重新判断
                out["window"] = self._sync_window(code, config.WINDOW_MINUTE_FREQ, config.DEFAULT_WINDOW,
                                                  max_requests=config.CATCHUP_MAX_REQUESTS)
            if out["window"]["accepted"] and market == "HK" and kind_of(code) == "stock":
                self.refresh_vendor_qfq(code, now)
        elif self._planned(code, "backfill"):
            with self._history_origin():             # 续传是历史来源：退避记 code#history，不连累盘中与定稿
                self.discover_missing(code)
                lo = (now.date() - timedelta(days=config.CATCHUP_LOOKBACK_DAYS)).isoformat()
                out["drain"] = self.drain_gaps(config.CATCHUP_MAX_REQUESTS, code=code, since=lo, codes={code})
                if out["drain"]["done"] and market == "HK" and kind_of(code) == "stock":
                    self.refresh_vendor_qfq(code, now, eligible=self.is_tracked)
        return out

    def catch_up_bounded(self, code, wait_s) -> bool:
        """门面调用：落后时在后台线程启动追赶并至多等待 wait_s 秒，返回是否启动过（调用方据此重读视图）。

        采集器禁用或数据已是最新（lagging 为假）时不启动、零请求；同一代码已有追赶在途时不等待；
        同一代码 CATCHUP_THROTTLE_S 内至多启动一次。超时后后台线程继续提交，结果由下一次读取看到。"""
        if not is_enabled():
            return False
        now = self.now()
        with self._catchup_guard:
            if code in self._refetching:
                return False
            running = self._catchups.get(code)
            last = self._catchup_at.get(code)
            if (running is not None and not running.is_set()) or (
                    last is not None and self.clock() - last < config.CATCHUP_THROTTLE_S):
                return False
        if not self.lagging(code, now):
            return False
        with self._catchup_guard:
            running = self._catchups.get(code)
            if running is not None and not running.is_set():
                return False
            done = self._catchups[code] = threading.Event()
            self._catchup_at[code] = self.clock()

        def run():
            try:
                self.catch_up(code, now)
            except Exception:  # noqa: BLE001 — 追赶失败只记日志，读侧照常服务现有视图
                log.warning("请求路径追赶失败 code=%s", code, exc_info=True)
            finally:
                conn = getattr(self._local, "conn", None)      # 一次性线程：关掉它自己的事实库连接
                if conn is not None:
                    self._local.conn = None
                    conn.close()
                done.set()

        threading.Thread(target=run, name=f"kline-catchup-{code}", daemon=True).start()
        done.wait(max(0.0, wait_s))
        return True

    def tick(self, now) -> dict:
        if not is_enabled():
            return {}
        ran = {}
        calendar_ran = False
        for market in ("CN", "HK"):
            today = now.date().isoformat()
            trading = self._trading(market, now)
            state = self._state.setdefault((market, today), {})
            budget = self._budget(bindings.binding(market, "any", FetchItem.SESSION_CALENDAR).primary)
            state["quota_ratio"] = budget.ratio() if budget else 0.0
            codes = [c for c in self.active_codes(now) if market_of(c) == market]
            modes = due_modes(now, market=market, is_trading_day=trading, state=state)
            if "BACKFILL" in modes:
                modes.remove("BACKFILL")                 # 历史工作在历史追赶线程（history_tick），调度线程不做
            for mode in modes:
                try:
                    if mode == "PREOPEN":
                        self._preopen_round(state, codes, now, active=self._still_active)
                        state["last_preopen"] = now
                        state["preopen_done"] = all(self.preopen_confirmed(c, today) for c in codes
                                                    if self._has_preopen_binding(c))
                    elif mode == "INTRADAY":
                        self.intraday_tick(self._intraday_codes(state, codes, now), now, active=self._still_active)
                        state["last_intraday"] = now
                        self._preopen_round(state, codes, now, active=self._still_active)  # F4 补确认：盘中启动、盘前失败、新加入
                    elif mode == "FINALIZE":
                        known = calendar.is_trading_day(self.conn(), market, today) is not None
                        watched = [c for c in self.watchlist_fn() if market_of(c) == market]
                        self.finalize_due(watched, today, now, calendar_known=known, eligible=self.is_tracked)
                        if market == "CN" and _CN_INDEX not in watched:     # 系统依赖：只定稿当日日线
                            self.finalize_due([_CN_INDEX], today, now, calendar_known=known, day_only=True)
                    elif mode == "CALENDAR":
                        self.refresh_calendar(market, now)
                        state["calendar_date"] = today
                        calendar_ran = True
                    elif mode == "KEEPALIVE" and market == "CN":
                        from chanapp.engine.kline import keepalive
                        keepalive.run(self.conn(), self.provider, now=now, writer=self.writer,
                                      hk_code=next((c for c in self.watchlist_fn() if market_of(c) == "HK"), None),
                                      minute_enabled=self.minute_enabled)
                        state["keepalive_date"] = today
                except CollectorLocked:
                    log.warning("写者锁被占用，跳过 %s/%s", market, mode)
                except Exception:  # noqa: BLE001 — 单个模式失败不拖垮调度线程
                    log.warning("采集模式失败 %s/%s", market, mode, exc_info=True)
            ran[market] = modes
        export = self.cache_dir / "selfcheck_calendar.json"
        if calendar_ran or not export.exists():
            try:
                calendar.export_selfcheck(self.conn(), export, year=now.year)
            except Exception:  # noqa: BLE001
                log.warning("selfcheck 日历导出失败", exc_info=True)
        return ran

    # ---- 历史追赶线程（计划 2026-09-29 D5）----
    def history_tick(self, now) -> dict:
        """历史追赶一轮：独立守护线程每 HISTORY_INTERVAL_S 秒调用一次（测试直接同步调用）。

        - 对账对象是跟踪集合（tracked_codes：自选加上证指数，后者不在自选时只有日线职责，排在最前：日历推导与前复权链
          依赖它）；搜索查看的代码不在其中；
          逐代码 try-acquire 单飞锁做历史规划（首次整段回填、分钟按月登记、按发现水位查新缺的交易日、港股首建
          供应商缓存；不限请求数），锁忙本轮跳过；再续传这些代码的缺口：跨代码按缺口结束点从新到旧排队（同一结束点
          上证指数在前），每代码至多 HISTORY_CODE_REQUESTS 个请求、全轮合计至多 HISTORY_ROUND_REQUESTS 个（_drain_active）；
        - 本轮所有上游请求按历史来源记单标的退避（code#history，见 _history_origin）；
        - 只补到昨天：规划、发现与续传的区间都止于昨天，结束于今天的缺口不取（计划 A 决定 8）；
        - 盘中也运行，但只处理活跃代码；全库缺口续传与港股供应商缓存扩展只在没有市场处于盘中窗口、且到了原
          BACKFILL 时段（非交易日，或 A 股 15:05、港股 16:15 之后）时进行，每市场每轮至多 HISTORY_DRAIN_REQUESTS 个请求；
        - 与调度线程隔离：慢的历史请求只占本线程，上游请求在写者锁外，写者锁内只做短提交；同一缺口与请求路径
          追赶之间按 gap_id 互斥（drain_gaps）。"""
        if not is_enabled():
            return {}
        with self._history_origin():
            return self._history_round(now)

    def _history_round(self, now) -> dict:
        self._flush_vendor_stale()                     # 写者锁被占时没落盘的重述证据（零请求）
        out = {"CN": {"codes": [], "library": False}, "HK": {"codes": [], "library": False}, "drained": 0}
        in_session = self._in_session(now)
        active = self.tracked_codes()
        ready = []                                     # 本轮拿到过单飞锁的代码：参与活跃代码续传
        for market in ("CN", "HK"):                    # 1. 历史规划：不设请求上限（新加入的代码几秒内开始整段回填）
            codes = [c for c in active if market_of(c) == market]
            planned = ([_CN_INDEX] if market == "CN" else []) + [c for c in codes if c != _CN_INDEX]
            try:
                for code in planned:
                    with self._try_flight(code) as got:
                        if not got:
                            continue                   # 同一代码的首取、规划或缓存刷新在途：本轮跳过
                        try:
                            self._plan_history(code)
                        except CollectorLocked:
                            raise
                        except Exception:  # noqa: BLE001 — 单个标的失败不挡住余下标的
                            log.warning("历史规划失败 code=%s", code, exc_info=True)
                    ready.append(code)
                    out[market]["codes"].append(code)
            except CollectorLocked:
                log.warning("写者锁被占用，跳过 %s 历史规划", market)
        try:                                           # 2. 活跃代码续传：跨代码按缺口新旧排队，全轮限量
            out["drained"] = self._drain_active(ready)
        except CollectorLocked:
            log.warning("写者锁被占用，跳过活跃代码续传")
        except Exception:  # noqa: BLE001
            log.warning("活跃代码续传失败", exc_info=True)
        for market in ("CN", "HK"):                    # 3. 全库续传与缓存扩展：只在没有市场开盘的回填时段
            try:
                trading = self._trading(market, now)
                if not in_session and "BACKFILL" in due_modes(now, market=market, is_trading_day=trading, state={}):
                    out[market]["library"] = True
                    self.drain_gaps(config.HISTORY_DRAIN_REQUESTS, market=market, codes=set(active))
                    for code in active:
                        if market_of(code) == market:
                            self._extend_vendor(code, now)    # 首开只建窗口，更早的历史在这里补进缓存
            except CollectorLocked:
                log.warning("写者锁被占用，跳过 %s 全库续传", market)
            except Exception:  # noqa: BLE001 — 一个市场失败不拖垮历史线程
                log.warning("历史追赶失败 %s", market, exc_info=True)
        return out

    def _drain_active(self, codes) -> int:
        """活跃代码的缺口续传：候选是这些代码的全部未解决缺口（known_gap 与结束于今天的除外），按结束点从新到旧排队，
        结束点相同时上证指数在前、其余按代码；逐条取，每代码至多 HISTORY_CODE_REQUESTS 个请求，全轮至多
        HISTORY_ROUND_REQUESTS 个。这样只缺昨天的代码不会被排在前面的大段积压饿住。返回本轮请求数。"""
        today = self.now().date().isoformat()
        conn = self.conn()
        candidates = [g for code in codes for g in facts.open_gaps(conn, code)
                      if g["reason"] != "known_gap" and g["end"][:10] < today and self._current_gap(g)]
        candidates.sort(key=lambda g: (g["code"] != _CN_INDEX, g["code"]))
        candidates.sort(key=lambda g: g["end"], reverse=True)      # 稳定排序：同一结束点保留上一行的次序
        per_code, total = Counter(), 0
        for gap in candidates:
            if total >= config.HISTORY_ROUND_REQUESTS:
                break
            if per_code[gap["code"]] >= config.HISTORY_CODE_REQUESTS or not self.eligible(gap["code"], gap["dataset"]):
                continue                               # 每条请求前复核跟踪资格（本轮中途移出自选即停）
            try:
                res = self.drain_gaps(1, gaps=[gap])
            except CollectorLocked:
                raise
            except Exception:  # noqa: BLE001 — 一条缺口失败不挡住余下缺口
                log.warning("历史缺口续传失败 code=%s gap=%s", gap["code"], gap["gap_id"], exc_info=True)
                per_code[gap["code"]] += 1             # 可能已发出请求：照样占上限
                total += 1
                continue
            n = res["done"] + res["failed"] + res["incomplete"]
            per_code[gap["code"]] += n
            total += n
            if res["budget_exhausted"]:
                break
        return total

    def _loop(self) -> None:
        while not self._stop.wait(5):
            try:
                self.tick(self.now())
            except Exception:  # noqa: BLE001
                log.warning("采集轮次异常", exc_info=True)

    def _history_loop(self) -> None:
        while not self._stop.wait(config.HISTORY_INTERVAL_S):
            try:
                self.history_tick(self.now())
            except Exception:  # noqa: BLE001
                log.warning("历史追赶轮次异常", exc_info=True)

    def start(self) -> bool:
        """起两个守护线程：调度（盘前、盘中、定稿、日历、保活）与历史追赶（D5），互不等待；只补起没在运行的那个。"""
        if not is_enabled():
            return False
        try:
            self.sync_minute_fact_freq()
        except CollectorLocked:
            log.warning("分钟事实粒度切换未执行：另一个写者持有写者锁（下次启动再做）")
        alive = lambda t: t is not None and t.is_alive()   # noqa: E731
        if alive(self._thread) and alive(self._history_thread):
            return False
        self._stop.clear()        # stop() 之后历史线程可能还卡在长请求里：清掉停止标记，它返回后继续循环，不另起一个
        if not alive(self._thread):
            self._thread = threading.Thread(target=self._loop, name="kline-collector", daemon=True)
            self._thread.start()
        if not alive(self._history_thread):
            self._history_thread = threading.Thread(target=self._history_loop, name="kline-history", daemon=True)
            self._history_thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        for thread in (self._thread, self._history_thread):
            if thread:
                thread.join(timeout=10)

    # ---- 状态汇总（计划 B 的 /api/status） ----
    def status(self, codes, *, now) -> dict:
        from chanapp.engine.kline import instance
        selected = instance.current()
        conn = self.conn()
        datasets = []
        for code in codes:
            fact = self._fact_freq(code)
            for dataset in (d for d in ("day", fact) if d is not None):
                stale, age, judged = views.dataset_stale(conn, code, dataset, now)
                datasets.append({
                    "code": code, "dataset": dataset,
                    "last_commit_at": facts.last_commit_at(conn, code, dataset),
                    "stale": stale, "stale_age_s": age, "stale_judged": judged,
                    "open_gaps": sum(g["reason"] != "known_gap" for g in facts.open_gaps(conn, code, dataset)),
                    "known_gaps": conn.execute("SELECT COUNT(*) FROM coverage_gaps WHERE code=? AND dataset=?"
                                               " AND reason='known_gap' AND resolved_at IS NULL",
                                               (code, dataset)).fetchone()[0],
                    "pending_review": conn.execute("SELECT COUNT(*) FROM pending_review WHERE code=? AND"
                                                   " dataset=? AND verdict IS NULL",
                                                   (code, dataset)).fetchone()[0],
                })
        probes = []
        for b in bindings.BINDINGS:
            active, gen = bindings.active(conn, b.market, b.kind, b.item)
            last = {}
            for prefix, source in (("", b.primary), ("keepalive-", b.cold)):
                row = conn.execute("SELECT verified_at FROM probe_runs WHERE probe_id=? AND market=?"
                                   " AND kind=? AND item=? ORDER BY run_id DESC LIMIT 1",
                                   (prefix + b.probe_id, b.market, b.kind, b.item.value)).fetchone()
                # 结论经 verdict_of：provider 契约变化后旧结论失效为 pending
                last[prefix or "probe"] = ({"verdict": bindings.verdict_of(conn, b.market, b.kind, b.item, source),
                                            "verified_at": row["verified_at"]} if row and source else None)
            probes.append({"market": b.market, "kind": b.kind, "item": b.item.value, "active": active,
                           "binding_gen": gen, "probe": last["probe"], "keepalive": last["keepalive-"]})
        budget = {}
        for source in config.QUOTA:
            b = self._budget(source)
            budget[source] = {"day": b.day(), "used": b.used(), "limit": b.per_day}
        return {"checked_at": now.isoformat(timespec="seconds"),
                "mode": selected.mode if selected is not None else "unconfigured",
                "enabled": is_enabled() and all(t is not None and t.is_alive()
                                                for t in (self._thread, self._history_thread)),
                "datasets": datasets, "probes": probes, "budget": budget,
                "calendar_export": str(self.cache_dir / "selfcheck_calendar.json")}


_shared: dict = {}
_shared_lock = threading.Lock()


def shared(cache_dir, *, watchlist_fn=None, minute_enabled=None) -> Collector:
    """按 cache_dir 缓存的进程级单例；之后传入的非 None watchlist_fn / minute_enabled 覆盖旧值。"""
    key = str(Path(cache_dir).resolve())
    with _shared_lock:
        worker = _shared.get(key)
        if worker is None:
            worker = _shared[key] = Collector(cache_dir, watchlist_fn=watchlist_fn, minute_enabled=minute_enabled)
        elif watchlist_fn is not None:
            worker.watchlist_fn = watchlist_fn
        if minute_enabled is not None:
            worker.minute_enabled = minute_enabled
        return worker
