"""长桥港股 provider（主源）：raw 日线与 m30 入事实层，另供供应商前复权序列。spec §5.1、§5.2、§7。

- 代码映射：hk00700 → "700.HK"（去前导零）。
- 日线：raw（NoAdjust），pc = 上一行收盘（港股前收即上一日收盘），首行用回看窗口补；currency HKD，volume 为股。
- m30：长桥是区间起始标签（09:30…15:30），加 30 分钟归一为末端标签。收市竞价 bar 的起始标签等于会话终点
  （常规日 16:00；半日市按同一规则落在 12:00），并入该终点槽：open 取前一根常规 bar，high/low 取两者极值，
  close 取竞价 bar，量额相加。常规日每日 11 根。开/收越出高低超过竞价补齐容差时，取同口径当日日线佐证（开/收等于
  日线开/收且在日线高低内）后补齐，不成立的留给准入拒收。个别交易日长桥 m30 从某一时刻起起始标签整体偏离半点
  （如 13:29、13:59，m5 同样偏移；m1 始终在整分钟上，正常日 m1 按 30 分钟分桶聚合与 m30 逐根相同）：这类日子只
  多取一次当日同口径 m1，按桶聚合补上缺的半点槽，已在半点上的 m30 保留原值，偏移的行丢弃；m1 也没有的桶留缺。
  长桥个别日子聚合 m30 时丢掉首个时间桶（如 1888.HK 2026-07-17 缺 09:30 起点那根）：历史与前复权取数时，当日最后一根
  之前缺的半点槽同样由 m1 聚合补，但只在 m1 有该桶起点那一分钟时补（2026-05-19 m1 也缺 09:30，不补）；m1 不含开市
  竞价量，补出的首桶量偏少。盘中增量不补（每轮都取，缺的槽交给定稿与续传）。
  其余起始标签不在会话内的行（盘前/盘后）逐行丢弃并记日志，不作废整批（spec §5.3：只隔离已证实错误的行）。
- minute_history 记 closed，且只收已过 now 的槽；minute_live 用 candlesticks(count=12)，只留当日行、一律 forming
  （计划 A 决定 8：closed 只来自定稿）。
- qfq_series：供应商前复权（ForwardAdjust），供港股派生缓存用，标签归一与竞价合并同 raw。
- m5 标签与竞价契约待 PH1 验证：抛 ProviderUnsupported。
- calendar(year)：港股年表（F5），trading_days 按 28 日分段取（SDK 区间须小于一个月、起点只到一年前；PH4 实测
  含未来交易日与半日市）。half_trading_days 不在 trading_days 里，记开市并只带上午会话。某段取数失败、或该段没有
  任何开市日（供应商尚未公布）时段内日子留作未知；晚于已知最后开市日的工作日也留作未知，其余未列出的工作日记休市。
- SDK（longbridge==5.1.0）lazy import；凭据只读 LONGBRIDGE_APP_KEY / LONGBRIDGE_APP_SECRET / LONGBRIDGE_ACCESS_TOKEN。
  SDK 不认 SOCKS 代理，需在可直连的网络环境运行。单测注入 client，不导入 SDK、不读凭据。
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from chanapp.engine.kline import sessions
from chanapp.engine.kline.providers.raw import (ProviderAuthError, ProviderConnectionError, ProviderError,
                                                ProviderRangeError, ProviderUnsupported)
from chanapp.engine.kline.rows import CalendarRow, RawDayRow, RawMinuteRow, new_batch_id

log = logging.getLogger(__name__)

CONTRACT_VERSION = "longbridge-raw-6"         # 2：naive 时间按主机时区解释；竞价价小幅越出时补齐高低；3：新增 calendar；
                                              # 4：m30 越界超出容差时由当日日线佐证补齐；5：起始标签不在半点上的日子由 m1 聚合补槽；
                                              # 6：历史与前复权 m30 当日最后一根之前缺的半点槽由 m1 聚合补（m1 有桶起点那一分钟时）
AUCTION_WIDEN_TOL = 0.01
_WIDENED_LOGGED: set = set()
HKT = ZoneInfo("Asia/Hong_Kong")
_CRED_NAMES = ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN")
LIVE_COUNT = 12
DAY_LOOKBACK_DAYS = 14          # 为首行 pc 回看的自然日（覆盖港股长假）
CHUNK_DAYS = {"day": 1000, "m30": 60, "m1": 2}   # 按日期分段请求，单段根数低于 SDK 单次上限（约 1000 根）
_SESSION_ENDS = {end for _, end in sessions.SESSIONS["HK"]}
_M30_GRID = set(sessions.slots("HK", "m30"))
CALENDAR_SPAN_DAYS = 28         # trading_days 单次区间（SDK 要求小于一个月，PH4 按 28 日实测）
CALENDAR_LOOKBACK_DAYS = 365    # trading_days 只回答最近一年
HALF_DAY_SESSIONS = (sessions.SESSIONS["HK"][0],)   # 半日市只开上午


def _symbol(code: str) -> str:
    return f"{int(code[2:])}.HK"


def _plain(value):
    """SDK 值 → JSON 友好：datetime → 港股本地 naive ISO，Decimal → str。

    SDK 的 naive datetime 是主机本地时间（V2 实测），astimezone 对 naive 值按主机时区解释。
    """
    if isinstance(value, datetime):
        return value.astimezone(HKT).replace(tzinfo=None).isoformat()
    if isinstance(value, (int, float, str)) or value is None:
        return value
    return str(value)


class SdkClient:
    """包装 longbridge SDK 的缺省 client；首次取数时才导入 SDK 并读凭据。"""

    def __init__(self):
        self._ctx = None
        self._ctx_lock = threading.Lock()

    def _context(self):
        """懒建 QuoteContext；调度、历史追赶与请求路径追赶可能同时首次取数，加锁只建一次（一个上下文一条长连接）。"""
        if self._ctx is None:
            with self._ctx_lock:
                if self._ctx is None:
                    creds = [os.environ.get(n, "") for n in _CRED_NAMES]
                    if not all(creds):
                        missing = [n for n, v in zip(_CRED_NAMES, creds) if not v]
                        raise ProviderAuthError("未设置", f"长桥凭据未设置: {missing}")   # 整个源不可用、不会自愈
                    from longbridge.openapi import Config, QuoteContext
                    config = Config.from_apikey(*creds, enable_print_quote_packages=False)
                    self._ctx = QuoteContext(config)
        return self._ctx

    @staticmethod
    def _enums(period, adjust):
        from longbridge.openapi import AdjustType, Period
        periods = {"day": Period.Day, "m30": Period.Min_30, "m5": Period.Min_5, "m1": Period.Min_1}
        adjusts = {"raw": AdjustType.NoAdjust, "qfq": AdjustType.ForwardAdjust}
        return periods[period], adjusts[adjust]

    @staticmethod
    def _rows(candles) -> list[dict]:
        return [{k: _plain(getattr(c, k)) for k in ("timestamp", "open", "high", "low", "close", "volume", "turnover")}
                for c in candles]

    def history_by_date(self, symbol, period, adjust, start, end) -> list[dict]:
        p, a = self._enums(period, adjust)
        candles = self._context().history_candlesticks_by_date(
            symbol, p, a, date.fromisoformat(start), date.fromisoformat(end))
        return self._rows(candles)

    def candlesticks(self, symbol, period, count, adjust) -> list[dict]:
        p, a = self._enums(period, adjust)
        return self._rows(self._context().candlesticks(symbol, p, count, a))

    def trading_days(self, begin, end) -> dict:
        from longbridge.openapi import Market
        r = self._context().trading_days(Market.HK, date.fromisoformat(begin), date.fromisoformat(end))
        return {"trading_days": [d.isoformat() for d in r.trading_days],
                "half_trading_days": [d.isoformat() for d in r.half_trading_days]}


def _bar(r: dict) -> dict:
    bar = {"open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
           "close": float(r["close"]), "volume": float(r["volume"]),
           "amount": None if r.get("turnover") in (None, "") else float(r["turnover"])}
    return _widen_for_auction(bar, r["timestamp"])


def _widen_for_auction(bar: dict, label: str) -> dict:
    """开/收盘价小幅越出高低时把高低扩到含它：港股开市前竞价与收市竞价在最终参考平衡价撮合，是真实成交，
    上游高低有时只统计持续交易（V2 实测 1888.HK 2023-06-13 开 7.48 > 高 7.47，独立来源同值）。
    越出超过 AUCTION_WIDEN_TOL 不补，留给准入按 bad_ohlc 拒收。"""
    hi, lo = max(bar["open"], bar["close"]), min(bar["open"], bar["close"])
    if bar["low"] <= lo and hi <= bar["high"]:
        return bar
    if bar["high"] <= 0 or bar["low"] <= 0 or bar["low"] > bar["high"]:
        return bar
    if hi > bar["high"] * (1 + AUCTION_WIDEN_TOL) or lo < bar["low"] * (1 - AUCTION_WIDEN_TOL):
        return bar
    key = (label, bar["high"], bar["low"], hi, lo)
    if key not in _WIDENED_LOGGED:            # 盘中轮询会反复取到同一根 bar，同一补齐只记一次
        if len(_WIDENED_LOGGED) > 4096:
            _WIDENED_LOGGED.clear()
        _WIDENED_LOGGED.add(key)
        log.warning("长桥 %s 开/收越出高低（竞价成交），高低 %s/%s → %s/%s", label, bar["high"], bar["low"],
                    max(hi, bar["high"]), min(lo, bar["low"]))
    return {**bar, "high": max(hi, bar["high"]), "low": min(lo, bar["low"])}


def _out_of_range(bar: dict) -> bool:
    """开或收越出高低，且高低本身可用（非正、低高倒置的不在此列，留给准入拒收）。"""
    if bar["high"] <= 0 or bar["low"] <= 0 or bar["low"] > bar["high"]:
        return False
    return bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(bar["open"], bar["close"])


def _widen_by_day(bar: dict, day: dict, label: str) -> dict:
    """越出超过容差的 m30 开/收，由同源同复权口径的当日日线佐证时补齐高低：越界的是开盘价则须等于日线开盘价，
    是收盘价则须等于日线收盘价，且落在日线高低内（6127.HK 2024-12-10 实测：m30 开 10.30 高于高 10.18 达 1.18%，
    日线开 = 高 = 10.30，持续交易最高只到 10.18）。佐证不成立的原样返回，留给准入按 bad_ohlc 拒收。"""
    for price, name in ((bar["open"], "open"), (bar["close"], "close")):
        if bar["low"] <= price <= bar["high"]:
            continue
        if price != day[name] or not day["low"] <= price <= day["high"]:
            return bar
    hi, lo = max(bar["open"], bar["close"]), min(bar["open"], bar["close"])
    log.warning("长桥 m30 %s 开/收越出高低超过容差，经当日日线佐证（竞价成交），高低 %s/%s → %s/%s",
                label, bar["high"], bar["low"], max(hi, bar["high"]), min(lo, bar["low"]))
    return {**bar, "high": max(hi, bar["high"]), "low": min(lo, bar["low"])}


def _hhmm_slot_end(hhmm: str) -> str:
    h, m = int(hhmm[:2]), int(hhmm[3:5]) + 30
    return f"{h + m // 60:02d}:{m % 60:02d}"


def _off_grid(r: dict) -> bool:
    """连续交易时段内、起始标签不在半点上的 m30 行（收市竞价与盘前盘后不算）。"""
    hhmm = r["timestamp"][11:16]
    in_session = any(start <= hhmm < end for start, end in sessions.SESSIONS["HK"])
    return in_session and _hhmm_slot_end(hhmm) not in _M30_GRID


_M30_STARTS = tuple(f"{int(end[:2]) - (end[3:] == '00'):02d}:{'30' if end[3:] == '00' else '00'}"
                    for end in sessions.slots("HK", "m30"))     # 末端标签 → 起始标签：10:00 → 09:30


def _missing_starts(rows: list[dict]) -> dict[str, list[str]]:
    """{日期: [缺的起始标签]}：当日最后一根半点槽之前没有的半点槽（收市竞价不算）。最后一根之后的不算缺（当天还没到、
    半日市只开上午）。"""
    have: dict[str, set] = {}
    for r in rows:
        hhmm = r["timestamp"][11:16]
        if hhmm in _M30_STARTS:
            have.setdefault(r["timestamp"][:10], set()).add(hhmm)
    out = {}
    for day, starts in have.items():
        gap = [s for s in _M30_STARTS if s < max(starts) and s not in starts]
        if gap:
            out[day] = gap
    return out


def _m1_buckets(rows: list[dict]) -> dict[str, dict]:
    """m1 → 按 30 分钟桶聚合的 m30 形状行（起始标签 → 行）；只聚合落在半点网格上的连续交易桶，收市竞价等不在桶内的行不取。"""
    groups: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda r: r["timestamp"]):
        hhmm = r["timestamp"][11:16]
        start = f"{hhmm[:2]}:{int(hhmm[3:5]) // 30 * 30:02d}"
        if not any(a <= hhmm < b for a, b in sessions.SESSIONS["HK"]) or _hhmm_slot_end(start) not in _M30_GRID:
            continue
        groups.setdefault(f"{r['timestamp'][:10]}T{start}:00", []).append(r)
    out = {}
    for ts, g in groups.items():
        turnover = [r.get("turnover") for r in g]
        out[ts] = {"timestamp": ts, "open": g[0]["open"], "close": g[-1]["close"],
                   "high": max((r["high"] for r in g), key=float), "low": min((r["low"] for r in g), key=float),
                   "volume": sum(float(r["volume"]) for r in g),
                   "turnover": None if any(t in (None, "") for t in turnover) else sum(float(t) for t in turnover)}
    return out


def _normalize_m30(candles: list[dict]) -> list[tuple[str, dict]]:
    """起始标签 → 末端标签；会话终点起始的收市竞价 bar 并入该终点槽。返回 [(slot_end, bar)]，按时间升序。"""
    grid = set(sessions.slots("HK", "m30"))
    out: list[tuple[str, dict]] = []
    for r in sorted(candles, key=lambda r: r["timestamp"]):
        ts = r["timestamp"].replace("T", " ")[:16]
        start = datetime.strptime(ts, "%Y-%m-%d %H:%M")
        slot = (start + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
        bar = _bar(r)
        if slot[11:] in grid:
            out.append((slot, bar))
            continue
        if ts[11:] in _SESSION_ENDS:              # 收市竞价：起始标签即会话终点
            if out and out[-1][0] == ts:
                prev = out[-1][1]
                merged = {"open": prev["open"], "high": max(prev["high"], bar["high"]),
                          "low": min(prev["low"], bar["low"]), "close": bar["close"],
                          "volume": prev["volume"] + bar["volume"],
                          "amount": None if prev["amount"] is None or bar["amount"] is None
                          else prev["amount"] + bar["amount"]}
                out[-1] = (ts, merged)
            else:
                out.append((ts, bar))
            continue
        log.warning("长桥 m30 起始标签 %s 不在港股会话内，丢弃（盘前/盘后或契约漂移）", ts)
    slots = [s for s, _ in out]
    if len(set(slots)) != len(slots):
        raise ProviderError("长桥 m30 归一后 slot_end 重复")
    return out


def _now_label(now) -> str:
    if now.tzinfo is not None:
        now = now.astimezone(HKT)
    return now.strftime("%Y-%m-%d %H:%M")


class LongbridgeProvider:
    name = "longbridge"
    CONTRACT_VERSION = CONTRACT_VERSION

    def __init__(self, client=None, today=None):
        self._client = client if client is not None else SdkClient()
        self._today = today or (lambda: datetime.now(HKT).date())

    def _call(self, method, *args) -> list[dict]:
        return list(self._invoke(method, *args) or [])

    def _invoke(self, method, *args):
        try:
            return getattr(self._client, method)(*args)
        except ProviderError:
            raise
        except (OSError, TimeoutError) as exc:      # 含 ConnectionError：连接类，冷却整个源
            raise ProviderConnectionError(f"长桥 {method}: {type(exc).__name__}: {exc}") from None
        except Exception as exc:  # noqa: BLE001 — 消息里不含凭据（SDK 异常只带错误码与说明）
            code = getattr(exc, "code", None)
            if code is not None and str(code).startswith("401"):   # 401xxx：凭据被拒（401004 token invalid）
                raise ProviderAuthError(code, getattr(exc, "message", "") or exc) from None
            raise ProviderError(f"长桥 {method}: {type(exc).__name__}: {exc}") from None

    def _history(self, code, period, adjust, first_day, last_day) -> list[dict]:
        out, cursor, last = [], date.fromisoformat(first_day), date.fromisoformat(last_day)
        while cursor <= last:
            chunk_end = min(last, cursor + timedelta(days=CHUNK_DAYS[period] - 1))
            out.extend(self._call("history_by_date", _symbol(code), period, adjust,
                                  cursor.isoformat(), chunk_end.isoformat()))
            cursor = chunk_end + timedelta(days=1)
        if any(not (first_day <= r["timestamp"][:10] <= last_day) for r in out):
            raise ProviderRangeError(f"{code} 长桥 {period} 返回越出 {first_day}..{last_day}")
        return out

    def _day_series(self, code, adjust, start, end) -> list[tuple[str, dict, float | None]]:
        lookback = (date.fromisoformat(start) - timedelta(days=DAY_LOOKBACK_DAYS)).isoformat()
        candles = sorted(self._history(code, "day", adjust, lookback, end), key=lambda r: r["timestamp"])
        out, prev_close, seen = [], None, set()
        for r in candles:
            day, bar = r["timestamp"][:10], _bar(r)
            if day in seen:
                raise ProviderError(f"{code} 长桥日线 {day} 重复")
            seen.add(day)
            if start <= day <= end:
                out.append((day, bar, prev_close))
            prev_close = bar["close"]
        return out

    def _m30_pairs(self, code, adjust, candles, *, fill_missing=False) -> list[tuple[str, dict]]:
        """m30 归一（_normalize_m30），再对仍越出高低的 bar 取同口径当日日线佐证（_widen_by_day）。
        只有出现越界 bar 时才请求日线，一次覆盖越界 bar 所在的日期区间。fill_missing（历史与供应商前复权，不含盘中
        增量）：当日最后一根之前缺的半点槽由同口径 m1 聚合补上（_fill_off_grid）。"""
        pairs = _normalize_m30(self._fill_off_grid(code, adjust, candles, fill_missing=fill_missing))
        dates = sorted({slot[:10] for slot, bar in pairs if _out_of_range(bar)})
        if not dates:
            return pairs
        days = {r["timestamp"][:10]: _bar(r) for r in self._history(code, "day", adjust, dates[0], dates[-1])}
        return [(slot, _widen_by_day(bar, days[slot[:10]], slot)
                 if _out_of_range(bar) and slot[:10] in days else bar) for slot, bar in pairs]

    def _fill_off_grid(self, code, adjust, candles, *, fill_missing=False) -> list[dict]:
        """当日同口径 m1 按桶聚合补槽，每个需要的日子只多取一次 m1：
        - 有起始标签不在半点上的日子：丢掉偏移的行，补上该日已取区间内缺的半点槽；
        - fill_missing 时，最后一根半点槽之前缺槽的日子（长桥个别日子聚合时丢掉首个时间桶，如 1888.HK 2026-07-17）：
          只在 m1 有该桶起点那一分钟时补（2026-05-19 m1 也缺 09:30 那一分钟，聚合出的是残缺桶，量与最低价对不上
          日线，所有者 2026-09-29 裁定不补）。m1 不含开市竞价量，补出的首桶量偏少。"""
        off = [r for r in candles if _off_grid(r)]
        keep = [r for r in candles if not _off_grid(r)]
        have = {r["timestamp"][:16] for r in keep}
        missing = _missing_starts(keep) if fill_missing else {}
        for day in sorted({r["timestamp"][:10] for r in off} | set(missing)):
            m1 = self._history(code, "m1", adjust, day, day)
            buckets = _m1_buckets(m1)
            filled = []
            if any(r["timestamp"][:10] == day for r in off):
                span = [r["timestamp"][:16] for r in candles if r["timestamp"][:10] == day]
                lo, hi = min(span), max(span)
                filled = [c for ts, c in buckets.items() if lo <= ts[:16] <= hi and ts[:16] not in have]
                log.warning("长桥 m30 %s 起始标签不在半点上（%s），改由 m1 聚合补槽 %s", day,
                            [r["timestamp"][11:16] for r in off if r["timestamp"][:10] == day],
                            [c["timestamp"][11:16] for c in filled])
            minutes = {r["timestamp"][:16] for r in m1}
            got = {c["timestamp"][:16] for c in filled}
            gap = [buckets[f"{day}T{s}:00"] for s in missing.get(day, ())
                   if f"{day}T{s}" not in got and f"{day}T{s}" in minutes and f"{day}T{s}:00" in buckets]
            if missing.get(day):
                log.warning("长桥 m30 %s 缺半点槽（起始 %s），由 m1 聚合补 %s", day, missing[day],
                            [c["timestamp"][11:16] for c in gap])
            keep.extend(filled + gap)
        return keep

    @staticmethod
    def _check_freq(fact_freq):
        if fact_freq == "m5":
            raise ProviderUnsupported("m5 待 PH1")
        if fact_freq != "m30":
            raise ProviderUnsupported(f"长桥分钟事实只取 m30，收到 {fact_freq}")

    def day_history(self, code, start, end) -> list:
        batch = new_batch_id()
        return [RawDayRow(code, day, b["open"], b["high"], b["low"], b["close"], b["volume"], "share",
                          b["amount"], "HKD", pc, 0, "final", batch)
                for day, b, pc in self._day_series(code, "raw", start, end)]

    def _minute_rows(self, code, pairs, state) -> list:
        batch = new_batch_id()
        return [RawMinuteRow(code, slot[:10], slot, b["open"], b["high"], b["low"], b["close"], b["volume"],
                             "share", b["amount"], state, "traded", batch) for slot, b in pairs]

    def minute_history(self, code, fact_freq, start, end, *, now) -> list:
        self._check_freq(fact_freq)
        pairs = self._m30_pairs(code, "raw", self._history(code, "m30", "raw", start[:10], end[:10]), fill_missing=True)
        limit = min(end, _now_label(now))
        return self._minute_rows(code, [(s, b) for s, b in pairs if start <= s <= limit], "closed")

    def minute_live(self, code, fact_freq, *, now) -> list:
        self._check_freq(fact_freq)
        today = _now_label(now)[:10]
        pairs = self._m30_pairs(code, "raw", self._call("candlesticks", _symbol(code), "m30", LIVE_COUNT, "raw"))
        return self._minute_rows(code, [(s, b) for s, b in pairs if s[:10] == today], "forming")

    def qfq_series(self, code, freq, start, end) -> list[dict]:
        """供应商前复权序列：day → {trade_date, …}；m30 → {trade_date, slot_end, …}；量为股。"""
        if freq == "day":
            return [{"trade_date": day, **b, "volume_unit": "share"}
                    for day, b, _ in self._day_series(code, "qfq", start, end)]
        if freq != "m30":
            raise ProviderUnsupported(f"长桥供应商前复权只取 day/m30，收到 {freq}")
        pairs = self._m30_pairs(code, "qfq", self._history(code, "m30", "qfq", start[:10], end[:10]), fill_missing=True)
        return [{"trade_date": s[:10], "slot_end": s, **b, "volume_unit": "share"}
                for s, b in pairs if start <= s <= end]

    def calendar(self, year) -> list:
        first = max(date(year, 1, 1), self._today() - timedelta(days=CALENDAR_LOOKBACK_DAYS))
        last = date(year, 12, 31)
        opened, half, covered, error = set(), set(), [], None
        cursor = first
        while cursor <= last:
            end = min(cursor + timedelta(days=CALENDAR_SPAN_DAYS - 1), last)
            try:
                body = self._invoke("trading_days", cursor.isoformat(), end.isoformat()) or {}
            except ProviderError as exc:
                error = exc
                log.warning("长桥 trading_days %s..%s 失败，段内日子留作未知: %s", cursor, end, exc)
                if isinstance(exc, ProviderConnectionError):
                    break                               # 整个源不可用：其余段也留作未知
            else:
                days, halfs = set(body.get("trading_days") or []), set(body.get("half_trading_days") or [])
                if days or halfs:                       # 空段 = 供应商尚未公布，不能推成休市
                    opened |= days
                    half |= halfs
                    covered.append((cursor, end))
            cursor = end + timedelta(days=1)
        if not covered:
            if error is not None:
                raise error
            return []
        horizon = max(opened | half)
        rows = []
        for a, b in covered:
            day = a
            while day <= b:
                iso = day.isoformat()
                if iso in half:
                    rows.append(CalendarRow("HK", iso, True, HALF_DAY_SESSIONS))
                elif iso in opened:
                    rows.append(CalendarRow("HK", iso, True))
                elif day.weekday() < 5 and iso <= horizon:
                    rows.append(CalendarRow("HK", iso, False))
                day += timedelta(days=1)
        return rows
