"""pytdx raw 冷备：A 股指数日线、个股与指数分钟（m15，回退用 m5），一律不复权。spec §5.1 F1/F2/F3、§5.2、§8。

- 指数日线走 get_index_bars，volume_unit="lot"；个股日线不在本冷备范围（个股日线冷备是 BaoStock）。
- 分钟个股走 get_security_bars、指数走 get_index_bars，volume_unit="share"；m15 未经真实源实测（冷备激活门槛之一）。
- 标签（D11a）：pytdx 分钟是末端标签，但部分主站把上午末根（11:30 收）标成 13:00（2026-09-14 盘中实测），
  另一些主站标 11:30（2026-09-26 录制）。本模块把 13:00 归一为 11:30；同日两者并存视为串包，抛 ProviderError。
- 盘前占位：服务端对未开盘时段预发 O=H=L=C=前收、vol 为 denormal（~5.9e-39）的行，按 vol < 1 丢弃。
- 回绕：volume ≥ 2**32 视为计数器回绕，整批抛 ProviderError，不入库。
- 分页：offset 0 为最新一页，向更早翻页直到越过请求起点或见底；越界行按范围过滤（pytdx 只能按偏移取）。
- history 行记 closed，live 行只留当日、记 forming（计划 A 决定 8）。
- 主站池、选站、连接与故障切换、category 映射由旧实现迁入（计划 A 决定 11），
  本模块不依赖旧实现，也不 import factors/adjust（输出不复权）。pytdx 库 lazy import。
- 串包（2026-09-26 根因：超时返回 None 被当作见底、迟到的页留在连接上被下一个请求读走）：
  连接用 raise_exception=True，None 同样按失败处理并断开连接，迟到响应随旧连接丢弃；
  翻页（offset > 0）只在取首页的同一连接上续取，不切站、不按寿命重连，中途失败整批放弃
  （不同主站最新一根可能不同，按偏移跨站拼接会错位或断档）。
- 其余激活门槛（m15 实测、盘中主站复跑、指数量口径、回绕 fixture，spec §8）不在本模块范围。
- 失败归类：连接、超时与多站连接失败抛 ProviderConnectionError（采集器计入单源冷却、不消耗缺口重试次数）；
  多站都只返回空数据、回绕、串包等抛 ProviderError。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from chanapp.engine.kline.providers.raw import ProviderConnectionError, ProviderError, ProviderUnsupported
from chanapp.engine.kline.rows import RawDayRow, RawMinuteRow, kind_of, new_batch_id

log = logging.getLogger(__name__)

CONTRACT_VERSION = "pytdx-raw-1"
CATEGORY = {"m5": 0, "m15": 1, "m30": 2, "m60": 3, "day": 9}   # 5/6 是周/月线（2026-08-26 实测）
PAGE = 800                  # pytdx 单次请求上限
LIVE_COUNT = 60             # 当日 m5 最多 48 根（m15 16 根），多取一点跨过占位
_MINUTE_FREQS = ("m5", "m15")
VOLUME_WRAP = 2 ** 32
MORNING_CLOSE_ALIAS = {"13:00": "11:30"}

# 种子池（2026-10-26 在两处不同网络同时实测仍能取 K 线的 4 站，按 TCP 延迟升序）。
# 原 2026-08-27 的 8 站到 2026-10-24 全部改回桩应答（握手正常，K 线 body 只有 2 字节、无数据）；
# pytdx 自带 hq_hosts 同样不可用，只作种子全灭时的兜底候选。
SEED_HOSTS = [
    ("114.28.173.133", 7709),
    ("shtdx.gtjas.com", 7709),
    ("139.9.43.31", 7709),
    ("sztdx.gtjas.com", 7709),
]
CONNECT_TIMEOUT = 3          # 秒；握手/请求超此判失败
HOST_MAX_FAILURES = 3        # 同站连败熔断阈值
HOST_COOLDOWN = 600          # 熔断冷却 10 分钟
CONN_MAX_AGE = 1800          # 连接存活满 30 分钟重连
CONN_MAX_SYMBOLS = 500       # 单连接累计请求满 500 次重连
FAILOVER_ATTEMPTS = 3


def _latest_trading_day(now: datetime | None = None) -> str:
    """最近交易日近似（按周一~周五；节假日由选站降级路径兜底）。"""
    now = now or datetime.now()
    d = now.date()
    if not (now.weekday() < 5 and (now.hour, now.minute) >= (9, 30)):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def _describe(exc: BaseException) -> str:
    """异常文本；pytdx 在 raise_exception=True 下把原因包成 TdxFunctionCallError("calling function error")，
    真正原因在 original_exception，一并写出。"""
    text = str(exc)
    orig = getattr(exc, "original_exception", None)
    if orig is not None:
        kind = type(orig)
        name = kind.__name__ if kind.__module__ == "builtins" else f"{kind.__module__}.{kind.__name__}"
        text += f" <- {name}: {orig}"
    return text


def _market_num(code: str) -> tuple[int, str]:
    """sh → 1、sz → 0。"""
    if code.startswith("sh"):
        return 1, code[2:]
    if code.startswith("sz"):
        return 0, code[2:]
    raise ProviderError(f"pytdx 不支持: {code}")


class _EmptyPage(RuntimeError):
    """首页空数据：连接正常，按标的级失败处理。"""


class _HostState:
    __slots__ = ("failures", "cool_until")

    def __init__(self):
        self.failures = 0
        self.cool_until = 0.0

    def allow(self) -> bool:
        return self.failures < HOST_MAX_FAILURES or time.monotonic() >= self.cool_until

    def record_failure(self):
        self.failures += 1
        if self.failures >= HOST_MAX_FAILURES:
            self.cool_until = time.monotonic() + HOST_COOLDOWN

    def record_success(self):
        self.failures = 0


class _HostPool:
    """惰性选站 + 单连接复用 + 同站熔断 + 多站故障切换（迁自旧 PytdxProvider 的连接管理）。"""

    def __init__(self):
        self._api = None
        self._host: tuple[str, int] | None = None
        self._connected_at = 0.0
        self._requests = 0
        self._conn_seq = 0                                # 每次断开递增：翻页只认取首页时的那条连接
        self._pages: dict[tuple, int] = {}               # (kind, category, market, num) → 首页所在连接
        self._ranked: list[tuple[str, int]] | None = None
        self._host_states: dict[tuple[str, int], _HostState] = {}
        self._lock = threading.Lock()

    def _host_state(self, host) -> _HostState:
        return self._host_states.setdefault(host, _HostState())

    def _candidate_hosts(self) -> list[tuple[str, int]]:
        hosts = list(SEED_HOSTS)
        try:
            from pytdx.config.hosts import hq_hosts
            for h in hq_hosts:
                hp = (h[1], h[2])
                if hp not in hosts:
                    hosts.append(hp)
        except Exception as e:  # noqa: BLE001
            log.warning("pytdx hq.hosts 读取失败: %s", e)
        return hosts

    def _probe_host(self, host) -> tuple[float, str] | None:
        """探活：connect + 首笔日线；返回 (总耗时, 末根 bar 日期)，失败 None。"""
        from pytdx.hq import TdxHq_API
        api = TdxHq_API()
        t0 = time.monotonic()
        try:
            if not api.connect(host[0], host[1], time_out=CONNECT_TIMEOUT):
                return None
            rows = api.get_security_bars(CATEGORY["day"], 0, "000001", 0, 5)
            rows = [r for r in rows or [] if float(r["vol"]) >= 1]
            if not rows:
                return None
            return time.monotonic() - t0, str(rows[-1]["datetime"])[:10]
        except Exception:  # noqa: BLE001
            return None
        finally:
            try:
                api.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def _select_hosts(self) -> list[tuple[str, int]]:
        """探种子池，握手+首笔耗时升序；新鲜度门不过时降级取数据最新者；种子全灭退全集候选。"""
        if self._ranked is not None:
            return self._ranked
        expect = _latest_trading_day()
        scored, relaxed = [], []
        for host in SEED_HOSTS:
            if not self._host_state(host).allow():
                continue
            r = self._probe_host(host)
            if r is None:
                self._host_state(host).record_failure()
                continue
            cost, last_dt = r
            (scored if last_dt == expect else relaxed).append((cost, host, last_dt))
        scored.sort()
        ranked = [h for _, h, _ in scored]
        if not ranked and relaxed:
            relaxed.sort(key=lambda t: (t[2], -t[0]), reverse=True)
            log.warning("pytdx 选站：无站过新鲜度门（期望 %s），降级取数据最新者", expect)
            ranked = [h for _, h, _ in relaxed]
        if not ranked:
            log.warning("pytdx 种子池全不可用，退全集候选")
            ranked = [h for h in self._candidate_hosts() if self._host_state(h).allow()]
        self._ranked = ranked
        return ranked

    def _disconnect(self):
        self._conn_seq += 1
        if self._api is not None:
            try:
                self._api.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._api = None

    def _connect_next(self):
        self._disconnect()
        ranked = self._select_hosts()
        if not ranked:
            raise ProviderConnectionError("pytdx 无可用主站")
        start = 0
        if self._host is not None and self._host in ranked:
            start = (ranked.index(self._host) + 1) % len(ranked)
        from pytdx.hq import TdxHq_API
        last_err = None
        for i in range(len(ranked)):
            host = ranked[(start + i) % len(ranked)]
            if not self._host_state(host).allow():
                continue
            api = TdxHq_API(raise_exception=True)
            try:
                if api.connect(host[0], host[1], time_out=CONNECT_TIMEOUT):
                    self._api, self._host = api, host
                    self._connected_at, self._requests = time.monotonic(), 0
                    return
            except Exception as e:  # noqa: BLE001
                last_err = e
            self._host_state(host).record_failure()
        raise ProviderConnectionError(f"pytdx 全部主站连接失败: {last_err}")

    def _ensure_conn(self):
        if (self._api is None or time.monotonic() - self._connected_at >= CONN_MAX_AGE
                or self._requests >= CONN_MAX_SYMBOLS):
            self._connect_next()

    def query(self, kind, category, market, num, offset, count) -> list[dict]:
        """单页取数。首页：异常或空数据 → 记站失败、切下一站重试，最多 3 站；翻页见 _continue。"""
        key = (kind, category, market, num)
        last_err, empty = None, False
        with self._lock:
            if offset > 0:
                return self._continue(key, offset, count)
            for _ in range(FAILOVER_ATTEMPTS):
                try:
                    self._ensure_conn()
                    page = self._bars(key, offset, count)
                    if not page:
                        raise _EmptyPage("空数据")
                    self._host_state(self._host).record_success()
                    self._requests += 1
                    self._pages[key] = self._conn_seq
                    return [dict(r) for r in page]
                except Exception as e:  # noqa: BLE001
                    last_err, empty = _describe(e), isinstance(e, _EmptyPage)
                    log.warning("pytdx %s cat=%s @%s 失败: %s，切站重试", num, category, self._host, last_err)
                    if self._host is not None:
                        self._host_state(self._host).record_failure()
                    self._disconnect()
        message = f"pytdx {num} cat={category} 多站重试失败: {last_err}"
        if empty:
            raise ProviderError(message)            # 各站都连上了、只是没有数据：标的级
        raise ProviderConnectionError(message)

    def _bars(self, key, offset, count):
        kind, category, market, num = key
        fn = self._api.get_index_bars if kind == "index" else self._api.get_security_bars
        page = fn(category, market, num, offset, count)
        if page is None:                            # raise_exception=False 的超时形态，按失败处理
            raise ProviderConnectionError("返回 None")
        return page

    def _continue(self, key, offset, count) -> list[dict]:
        """翻页：必须仍是取首页的那条连接；空页即见底；失败断开连接、整批放弃，不切站。"""
        num, category = key[3], key[1]
        if self._api is None or self._pages.get(key) != self._conn_seq:
            raise ProviderConnectionError(f"pytdx {num} cat={category} 翻页时连接已变化，整批放弃")
        try:
            page = self._bars(key, offset, count)
        except Exception as e:  # noqa: BLE001
            reason = _describe(e)
            log.warning("pytdx %s cat=%s @%s 翻页失败: %s，断开连接、整批放弃", num, category, self._host, reason)
            self._host_state(self._host).record_failure()
            self._disconnect()
            raise ProviderConnectionError(f"pytdx {num} cat={category} 翻页失败: {reason}") from None
        self._requests += 1
        return [dict(r) for r in page]


_pool: _HostPool | None = None
_pool_lock = threading.Lock()


def default_query(kind, category, market, num, offset, count) -> list[dict]:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _HostPool()
    return _pool.query(kind, category, market, num, offset, count)


def _minute_label(raw_dt: str) -> str:
    stamp = str(raw_dt)[:16]
    hhmm = stamp[11:]
    return f"{stamp[:10]} {MORNING_CLOSE_ALIAS.get(hhmm, hhmm)}"


class PytdxRawProvider:
    name = "pytdx"
    CONTRACT_VERSION = CONTRACT_VERSION

    def __init__(self, query=None, page: int = PAGE):
        self._query = query or default_query
        self._page = page

    def _fetch(self, code, category, key, stop_before: str | None, count: int | None = None) -> list[dict]:
        """从最新页向前翻，直到最早一行早于 stop_before 或见底；返回按时间升序、已滤占位的原始行。"""
        kind = kind_of(code)
        market, num = _market_num(code)
        pages, offset = [], 0
        while True:
            n = count or self._page
            try:
                page = self._query(kind, category, market, num, offset, n)
            except ProviderError:
                raise
            except (TimeoutError, OSError) as exc:
                raise ProviderConnectionError(f"pytdx {code}: {type(exc).__name__}: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"pytdx {code}: {type(exc).__name__}: {exc}") from exc
            page = list(page or [])
            pages.insert(0, page)
            if count is not None or len(page) < n or not page:
                break
            if stop_before is not None and key(page[0]) < stop_before:
                break
            offset += n
        rows = [r for p in pages for r in p if float(r["vol"]) >= 1]   # 盘前占位
        for r in rows:
            if float(r["vol"]) >= VOLUME_WRAP:
                raise ProviderError(f"pytdx {code} {r['datetime']} 成交量 {r['vol']} ≥ 2**32，疑似回绕")
        return rows

    def day_history(self, code, start, end) -> list:
        if kind_of(code) != "index":
            raise ProviderUnsupported(f"pytdx 冷备日线只服务指数（个股日线冷备是 BaoStock）：{code}")
        raw = self._fetch(code, CATEGORY["day"], lambda r: str(r["datetime"])[:10], start)
        batch, rows, prev_close, seen = new_batch_id(), [], None, set()
        for r in sorted(raw, key=lambda r: str(r["datetime"])):
            day = str(r["datetime"])[:10]
            if day in seen:
                raise ProviderError(f"pytdx {code} 日线 {day} 重复")
            seen.add(day)
            close = float(r["close"])
            if start <= day <= end:
                rows.append(RawDayRow(code, day, float(r["open"]), float(r["high"]), float(r["low"]), close,
                                      float(r["vol"]), "lot", float(r["amount"]), "CNY", prev_close, 0,
                                      "final", batch))
            prev_close = close
        return rows

    def _minute_rows(self, code, raw, state) -> list:
        batch, rows = new_batch_id(), []
        for r in raw:
            slot = _minute_label(r["datetime"])
            rows.append(RawMinuteRow(code, slot[:10], slot, float(r["open"]), float(r["high"]), float(r["low"]),
                                     float(r["close"]), float(r["vol"]), "share", float(r["amount"]),
                                     state, "traded", batch))
        rows.sort(key=lambda r: r.slot_end)
        slots = [r.slot_end for r in rows]
        if len(set(slots)) != len(slots):
            dup = sorted({s for s in slots if slots.count(s) > 1})
            raise ProviderError(f"pytdx {code} 归一后 slot_end 重复：{dup[:3]}")
        return rows

    @staticmethod
    def _check_freq(fact_freq):
        if fact_freq not in _MINUTE_FREQS:
            raise ProviderUnsupported(f"pytdx 冷备分钟只取 m15/m5，收到 {fact_freq}")

    def minute_history(self, code, fact_freq, start, end, *, now) -> list:
        self._check_freq(fact_freq)
        raw = self._fetch(code, CATEGORY[fact_freq], lambda r: _minute_label(r["datetime"]), start)
        limit = min(end, now.strftime("%Y-%m-%d %H:%M"))
        return [r for r in self._minute_rows(code, raw, "closed") if start <= r.slot_end <= limit]

    def minute_live(self, code, fact_freq, *, now) -> list:
        self._check_freq(fact_freq)
        raw = self._fetch(code, CATEGORY[fact_freq], lambda r: _minute_label(r["datetime"]), None, count=LIVE_COUNT)
        today = now.date().isoformat()
        return [r for r in self._minute_rows(code, raw, "forming") if r.trade_date == today]
