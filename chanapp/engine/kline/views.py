"""一致视图（spec §6）：一次短读事务取齐依赖，计算前复权、周期、令牌、提示与来源。

视图不是取数项；本模块只读事实库。
- 前复权只在 A 股个股上按等比因子链计算，覆盖区外的 bar 不以前复权提供；指数恒为原始价；
- 分钟周期由事实粒度按会话钟点桶聚合，细于事实粒度的周期返回 unsupported；
- 令牌只跟随已收盘代次（closed_gen）、绑定代次、判定日期与当日确认状态、所读日历区段、运行身份；
- source / degraded 描述被服务数据的批次来源，不是当前绑定（计划 B 门面契约）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from chanapp.engine.kline import bindings, calendar, config, facts, hk_vendor_qfq, instance, periods, qfq, sessions
from chanapp.engine.kline.rows import FetchItem, kind_of, market_of, to_shares

PERIODS = ("day", "week", "m60", "m30", "m15", "m5")
MINUTE_PERIODS = ("m60", "m30", "m15", "m5")
ADJUSTS = ("qfq", "raw")
_LABEL = {"qfq": "前复权", "raw": "不复权", "vendor": "前复权（供应商口径）"}
_NOTICE_TEXT = {
    "qfq_from": "前复权自 {x} 起可用",
    "today_unconfirmed": "今日除权信息待确认，前复权显示至 {x}",
    "backfill_pending": "更早分钟历史加载中",
    "structure_short": "结构输入尚未补齐",
    "unsupported": "该市场暂不提供",
}
_BAR_KEYS = ("dt", "open", "high", "low", "close", "volume", "forming")


@dataclass
class View:
    code: str
    freq: str
    adjust: str
    bars: list
    token: str
    adjust_label: str
    source: str
    coverage: dict
    notices: list = field(default_factory=list)
    incomplete_days: list = field(default_factory=list)
    has_more: bool = False
    oldest_dt: str | None = None
    stale: bool = False
    stale_age_s: int | None = None
    last_commit_at: str | None = None
    degraded: bool = False
    volume_unit: str = "share"


def _notice(code, x=None) -> dict:
    return {"code": code, "text": _NOTICE_TEXT[code].format(x=x)}


class _Ctx:
    """一次读取的上下文：日期、市场、日线、因子链、事实粒度、冷备来源。"""

    def __init__(self, conn, code, adjust, now):
        if adjust not in ADJUSTS:
            raise ValueError(f"unsupported adjust: {adjust}")
        self.conn, self.code = conn, code
        self.market, self.kind = market_of(code), kind_of(code)
        self.adjust = "raw" if self.kind == "index" else adjust
        self.day_rows = facts.read_day_rows(conn, code)
        if instance.is_demo():
            # 纯历史读取的判定时点随样本固定。越过最后一周，不把聚合周当作当前盘中；
            # 缺根/复权覆盖仍由同一日历与质量规则判断，不以运行当天补造样本。
            latest = conn.execute("SELECT MAX(trade_date) FROM (SELECT trade_date FROM current_day_bars "
                                  "WHERE code=? UNION ALL SELECT trade_date FROM minute_bars WHERE code=?)",
                                  (code, code)).fetchone()[0]
            self.now = datetime.fromisoformat(latest or "1970-01-01") + timedelta(days=7)
            self.through = latest       # 样本截止日：之后日历上的交易日不是该样本的缺根
        else:
            self.now = now or datetime.now()
            self.through = None
        self.today = self.now.date().isoformat()
        start = self.day_rows[0]["trade_date"] if self.day_rows else self.today
        end = (self.now.date() + timedelta(days=7)).isoformat()
        self.trading_days = calendar.trading_days(conn, self.market, start, end)
        self.closed_days = calendar.closed_days(conn, self.market, start, end)
        self.is_trading_today = _is_trading(conn, self.market, self.now.date())
        self.fact_freq = bindings.binding(self.market, self.kind,
                                          FetchItem.MINUTE_HISTORY).minute_fact_freq
        self.chain = (qfq.build_chain(self.day_rows, self.trading_days, today=self.today,
                                      closed_days=self.closed_days)
                      if self.adjust == "qfq" and self.market == "CN" else None)
        self.vendor = self.adjust == "qfq" and self.market == "HK"
        self.cold = {bindings.binding(self.market, self.kind, item).cold
                     for item in (FetchItem.DAY_HISTORY, FetchItem.MINUTE_HISTORY,
                                  FetchItem.MINUTE_LIVE)} - {None}
        self._memo, self._minutes = {}, {}

    # 以下读取只在本上下文（同一读事务、同一快照）内复用，不跨请求保留
    def memo(self, key, fn):
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    def minute_rows(self, fact_freq, start_slot=None, end_slot=None, *, desc=False, limit=None) -> list:
        """facts.read_minute_rows 的复用版，结果与直接读取相同；返回的列表只读。

        参数完全相同直接复用，另有几种可证等价的截取（同一快照；每槽只取最大修订，按 slot_end 全序；隔离过滤逐行
        进行，与按 slot_end 截取可交换）。截不出时照常读库。"""
        key = (fact_freq, start_slot, end_slot, desc, limit)
        if key not in self._minutes:
            rows = self._derive(*key)
            if rows is None:
                rows = facts.read_minute_rows(self.conn, self.code, fact_freq, start_slot, end_slot,
                                              desc=desc, limit=limit)
            self._minutes[key] = rows
        return self._minutes[key]

    def _derive(self, fact_freq, start_slot, end_slot, desc, limit):
        for (freq, start, end, d, n), cached in self._minutes.items():
            if freq != fact_freq:
                continue
            if not desc and not limit and start_slot is not None:
                # 无上限的升序区间 [start_slot, end_slot)：
                # - 已读过同一终点、起点不晚的无上限区间：按起点截取；
                # - 已读过不设起点、终点不早的「最近 n 槽」：其最早一行不晚于 start_slot 时，区间内的每一槽都在这 n
                #   槽之内（最近 n 槽即 slot_end 不小于第 n 大者的全部槽），按起止截取。
                if not d and not n and end == end_slot and (start is None or start <= start_slot):
                    return [r for r in cached if r["slot_end"] >= start_slot]
                covers = end is None or (end_slot is not None and end_slot <= end)
                if d and n and start is None and covers and cached and cached[0]["slot_end"] <= start_slot:
                    return [r for r in cached
                            if r["slot_end"] >= start_slot and (end_slot is None or r["slot_end"] < end_slot)]
            elif desc and limit and start_slot is None:
                # 最近 limit 槽：已读过同一终点、上限不小的最近 n 槽，且没有隔离槽（过滤前后行数相同）时取其最近
                # limit 行；有隔离槽时过滤后的行数不能还原读库时的截断位置，不截取。
                if d and n and n >= limit and start is None and end == end_slot and not self.hidden(fact_freq):
                    return cached[-limit:]
        return None

    def hidden(self, dataset) -> set:
        return self.memo(("hidden", dataset), lambda: facts.quarantined_keys(self.conn, self.code, dataset))

    def required_slots(self, fact_freq, day) -> set:
        return self.memo(("required_slots", fact_freq, day),
                         lambda: calendar.required_slots(self.conn, self.market, fact_freq, day))

    def multiplier(self, trade_date):
        if self.adjust == "raw":
            return 1.0
        return self.chain.multipliers.get(trade_date) if self.chain else None


def _scale(bar, m) -> dict:
    out = dict(bar)
    for key in ("open", "high", "low", "close"):
        out[key] = bar[key] * m
    return out


def _apply(ctx, bars, *, raw=False) -> list:
    out = []
    for bar in bars:
        m = 1.0 if raw else ctx.multiplier(bar["trade_date"])
        if m is not None:
            out.append(_scale(bar, m))
    return out


def _day_bars(ctx, *, raw=False) -> list:
    bars, have_today = [], False
    for r in ctx.day_rows:
        if r["provenance"] not in ("live", "final") or r["sf"] != 0 or r["close"] is None:
            continue
        have_today = have_today or r["trade_date"] == ctx.today
        bars.append({"dt": r["trade_date"], "trade_date": r["trade_date"], "open": r["open"],
                     "high": r["high"], "low": r["low"], "close": r["close"],
                     "volume": to_shares(r["volume"], r["volume_unit"]) or 0, "amount": r["amount"],
                     "forming": r["provenance"] == "live", "source": r["source"],
                     "sources": [r["source"]]})
    if not have_today and ctx.fact_freq:
        minutes = ctx.minute_rows(ctx.fact_freq, f"{ctx.today} 00:00", f"{ctx.today} 23:59")
        bar = periods.day_bar_from_minutes(minutes, trade_date=ctx.today, forming=True)
        if bar:
            bars.append(bar)
    return _apply(ctx, bars, raw=raw)


def _minute_bars(ctx, freq, before, limit) -> tuple:
    fact = ctx.fact_freq
    target, base = sessions.FREQ_MINUTES[freq], sessions.FREQ_MINUTES[fact]
    if target < base or target % base:
        raise periods.UnsupportedPeriod(ctx.market, freq)
    read_limit = limit * (target // base) + len(sessions.slots(ctx.market, fact))
    rows = ctx.minute_rows(fact, end_slot=before, desc=True, limit=read_limit)
    bars = periods.aggregate_minutes(rows, market=ctx.market, fact_freq=fact, target_freq=freq)
    if before:
        bars = [b for b in bars if b["dt"] < before]      # 与分页锚点同桶的残段不出
    if len(rows) == read_limit and bars:
        bars = bars[1:]                                   # 读取上限处的最早一桶可能不完整
    bars = _apply(ctx, bars)
    kept = bars[-limit:]
    has_more = len(bars) > limit or len(rows) == read_limit
    if ctx.chain and kept and kept[0]["trade_date"] == ctx.chain.qfq_from and len(bars) <= limit:
        has_more = False                                  # 更早的分钟不在前复权覆盖区
    return kept, has_more


def _week_bars(ctx, before, limit) -> tuple:
    suspended = {r["trade_date"] for r in ctx.day_rows if r["sf"] == 1 and r["provenance"] != "preopen"}
    weeks, incomplete = periods.week_bars(_day_bars(ctx), trading_days=ctx.trading_days,
                                          suspended=suspended, today=ctx.today)
    if before:                                           # 分页先按整周截，再取最后 limit 周
        cut = sessions.week_start(before[:10])
        weeks = [w for w in weeks if w["week_start"] < cut]
        incomplete = [w for w in incomplete if w["week"] < cut]
    return weeks[-limit:], len(weeks) > limit, incomplete


def _vendor_bars(ctx, freq, before, limit) -> tuple:
    """港股过渡期前复权（spec §7 方案 (b)）：服务供应商缓存的已发布版本。

    只有缓存新鲜（未冻结、未标 stale、且覆盖到今日之前的全部 raw 收盘）时，才把今日盘中的 bar 以原始价接上；
    缓存落后于已收盘 raw、刷新失败或冻结时只服务到缓存末端并标 stale，避免跨越除净日混用复权基准。
    返回 (bars, has_more, week_incomplete, meta)，meta["lagging"] 表示缓存落后于今天之前的已收盘 raw，meta["through"]
    为缓存末端日期（状态栏据此判断收盘后是否追到当天）；缓存未建时 meta 为 None。
    """
    if freq in ("m15", "m5"):
        raise periods.UnsupportedPeriod(ctx.market, freq)
    base = "day" if freq in ("day", "week") else "m30"
    cached, meta = hk_vendor_qfq.read(ctx.conn, ctx.code, base)
    if meta is None:                                     # 支持但缓存未建：空视图，门面据此同步首取；不回落 raw
        return [], False, [], None
    source = bindings.binding(ctx.market, ctx.kind, FetchItem.DAY_HISTORY).primary
    last = cached[-1]["dt"] if cached else ""
    for bar in cached:
        bar.update(amount=None, forming=False, source=source, sources=[source])
    if base == "day":
        tail = [b for b in _day_bars(ctx, raw=True) if b["dt"] > last]
    else:
        tail = [r for r in ctx.minute_rows("m30") if r["slot_end"] > last]
    meta = {**meta, "lagging": any(b["trade_date"] < ctx.today for b in tail), "through": last[:10]}
    if meta["frozen"] or meta["stale"] or meta["lagging"]:
        tail = []
    meta["tail"] = bool(tail)                            # 展示里有没有 raw 尾巴（状态栏时间据此取依赖）
    if base == "day":
        bars = cached + tail
        if freq == "week":
            suspended = {r["trade_date"] for r in ctx.day_rows if r["sf"] == 1 and r["provenance"] != "preopen"}
            weeks, incomplete = periods.week_bars(bars, trading_days=ctx.trading_days, suspended=suspended,
                                                  today=ctx.today)
            if before:
                cut = sessions.week_start(before[:10])
                weeks = [w for w in weeks if w["week_start"] < cut]
                incomplete = [w for w in incomplete if w["week"] < cut]
            return weeks[-limit:], len(weeks) > limit, incomplete, meta
    else:
        rows = [{**b, "slot_end": b["dt"], "trade_state": "traded", "state": "closed", "volume_unit": "share"}
                for b in cached] + tail
        bars = periods.aggregate_minutes(rows, market=ctx.market, fact_freq="m30", target_freq=freq)
    if before:
        bars = [b for b in bars if b["dt"] < before]
    return bars[-limit:], len(bars) > limit, [], meta


def _is_trading(conn, market, day) -> bool:
    """日历未知（港股当日在定稿前通常如此）时按工作日判，与采集器的取数判定一致。"""
    known = calendar.is_trading_day(conn, market, day.isoformat())
    return known if known is not None else day.weekday() < 5


def _to_local(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone().replace(tzinfo=None)


def _finalized(conn, code, dataset, day) -> bool:
    """已定稿且可读：隔离的 final 日线或隔离槽不算。"""
    if dataset == "day":
        return (day not in facts.quarantined_keys(conn, code, "day")
                and conn.execute("SELECT 1 FROM current_day_bars WHERE code=? AND trade_date=?"
                                 " AND provenance='final'", (code, day)).fetchone() is not None)
    rows = facts.read_minute_rows(conn, code, dataset, f"{day} 00:00", f"{day} 23:59")
    return bool(rows) and all(r["state"] != "forming" for r in rows)


def dataset_stale(conn, code, dataset, now) -> tuple:
    """stale 新定义（spec §6.5）：按距最近一次成功提交的时刻与会话阶段判定，返回 (stale, 秒, judged)。

    judged 表示当前时段是否判定 stale：为 False 时 stale 恒为 False，不是「数据新鲜」的结论。

    - 交易时段：分钟数据集距最近提交超过 STALE_IN_SESSION_S；日线数据集盘中不判（当日 bar 来自分钟）；
    - 交易日该市场的 FINALIZE_DEADLINE 之后：当日未定稿（日线无 final 行 / 分钟仍有 forming）；
    - 交易日开盘后到定稿截止之间（含午休）：不判；
    - 非交易日与交易日开盘前：最近一个过去交易日未定稿。
    """
    if instance.is_demo() or dataset is None:
        return False, None, False
    market = market_of(code)
    last = facts.last_commit_at(conn, code, dataset)
    age = int((now - _to_local(last)).total_seconds()) if last else None
    today, hhmm = now.date().isoformat(), now.strftime("%H:%M")
    trading = _is_trading(conn, market, now.date())
    windows = sessions.SESSIONS[market]
    if trading and any(start <= hhmm <= end for start, end in windows):
        stale = dataset != "day" and (age is None or age > config.STALE_IN_SESSION_S)
        judged = dataset != "day"
    elif trading and hhmm >= config.FINALIZE_DEADLINE[market]:
        stale = not _finalized(conn, code, dataset, today)
        judged = True
    elif trading and hhmm >= windows[0][0]:
        stale = False
        judged = False
    else:
        prev = conn.execute("SELECT MAX(date) AS d FROM calendar WHERE market=? AND is_open=1 AND date<?",
                            (market, today)).fetchone()["d"]
        stale = prev is not None and not _finalized(conn, code, dataset, prev)
        judged = True
    return stale, (age if stale else None), judged


def _local_minute(ts: str | None) -> str | None:
    return _to_local(ts).strftime("%Y-%m-%d %H:%M") if ts else None


def _day_complete(ctx, day, minute) -> bool:
    """目标日当前图所需的数据已定稿：可读 final 日线；分钟图另要求当日应有槽位都已收盘可读（停牌日 sf=1 免分钟）；
    且没有待裁决（与采集器定稿判据共用 facts.day_unsettled：日线待核验、现行粒度的分钟日线核对不一致；分钟图另看
    现行粒度的分钟待核验）。"""
    if not _finalized(ctx.conn, ctx.code, "day", day):
        return False
    if facts.day_unsettled(ctx.conn, ctx.code, day, ctx.fact_freq, minutes=minute):
        return False
    if not minute or not ctx.fact_freq:
        return True
    if any(r["trade_date"] == day and r["sf"] == 1 and r["provenance"] == "final" for r in ctx.day_rows):
        return True
    rows = ctx.minute_rows(ctx.fact_freq, f"{day} 00:00", f"{day} 23:59")
    closed = {r["slot_end"] for r in rows if r["state"] == "closed"}
    return (not any(r["state"] == "forming" for r in rows)
            and ctx.required_slots(ctx.fact_freq, day) <= closed)


def _data_status(ctx, freq, vendor_meta) -> dict:
    """状态栏（目标 2026-09-29 第三阶段）：{phase, day, at}，与 bars 出自同一次读取。

    - live：交易日开盘到收盘（含午休），at 为分钟事实最后一次成功接纳的时刻；
    - 其余时段看「最近应完成交易日」day（收盘后是今天，开盘前与休市日是上一交易日）：当前图所需数据已定稿为
      final，否则 awaiting_final（分钟图要求分钟齐，仅日线成功不算；港股前复权另要求供应商缓存已追上）；
    - at 取当前图依赖的数据集最后一次成功接纳的时刻（失败、空返回、全拒不推进）；从未接纳为 none、at 为空。
      港股前复权图展示供应商缓存：at 取缓存发布时刻，仍展示 raw 尾巴时取两者较晚者（缓存冻结、stale 或落后时尾巴
      不展示，时间也不跟 raw）。"""
    hhmm = ctx.now.strftime("%H:%M")
    windows = sessions.SESSIONS[ctx.market]
    minute = freq in MINUTE_PERIODS
    live = ctx.is_trading_today and windows[0][0] <= hhmm <= windows[-1][1]
    at = _local_minute(facts.last_commit_at(ctx.conn, ctx.code, ctx.fact_freq if (live or minute) and ctx.fact_freq
                                            else "day"))
    if vendor_meta:                                      # 港股前复权图展示供应商缓存（加上仍在展示的 raw 尾巴）
        published = _local_minute(vendor_meta["fetched_at"])
        at = max(published, at) if vendor_meta["tail"] and at else published
    if live:
        return {"phase": "live" if at else "none", "day": ctx.today, "at": at}
    if ctx.is_trading_today and hhmm > windows[-1][1]:
        day = ctx.today
    else:
        past = [d for d in ctx.trading_days if d < ctx.today]
        day = past[-1] if past else None
    if at is None:
        return {"phase": "none", "day": day, "at": None}
    done = day is not None and _day_complete(ctx, day, minute)
    if done and vendor_meta and (vendor_meta["frozen"] or vendor_meta["stale"] or vendor_meta["lagging"]
                                 or vendor_meta["through"] < day):
        done = False                                     # 港股前复权：供应商缓存还没追到目标日
    return {"phase": "final" if done else "awaiting_final", "day": day, "at": at}


def _stale_dataset(ctx, freq) -> str | None:
    """判定 stale 的数据集；View.last_commit_at（门面的 fetch_time）必须取自同一数据集。"""
    hhmm = ctx.now.strftime("%H:%M")
    in_session = ctx.is_trading_today and any(s <= hhmm <= e for s, e in sessions.SESSIONS[ctx.market])
    if freq in ("day", "week"):
        return ctx.fact_freq if in_session and ctx.fact_freq else "day"
    return ctx.fact_freq


def _backfill_notices(ctx, count, limit) -> list:
    # known_gap 已不再自动补取，不能让「加载中」一直挂着
    gaps = ctx.memo(("open_gaps", ctx.fact_freq), lambda: facts.open_gaps(ctx.conn, ctx.code, ctx.fact_freq))
    if not any(g["reason"] != "known_gap" for g in gaps):
        return []
    out = [_notice("backfill_pending")]
    if count < limit:
        out.append(_notice("structure_short"))
    return out


def _incomplete_days(ctx, bars) -> list:
    """过去交易日的质量标记，按实际可读的分钟集合统计（隔离槽不计）：
    槽位不足或整日无分钟（停牌日除外）、仍有 forming（定稿失败）、分钟与日线或槽位冲突待核验。"""
    first = bars[0]["dt"][:10]
    by_day = {}
    for r in ctx.minute_rows(ctx.fact_freq, f"{first} 00:00", f"{ctx.today} 00:00"):
        n, forming = by_day.get(r["trade_date"], (0, False))
        by_day[r["trade_date"]] = (n + 1, forming or r["state"] == "forming")
    suspended = {r["trade_date"] for r in ctx.day_rows if r["sf"] == 1 and r["provenance"] != "preopen"}
    for d in ctx.trading_days:
        if first <= d < ctx.today and (ctx.through is None or d <= ctx.through) and d not in suspended:
            by_day.setdefault(d, (0, False))
    out = []
    for d in sorted(by_day):
        n, forming = by_day[d]
        if forming:
            out.append({"date": d, "kind": "forming", "slots": n})
        elif n < len(ctx.required_slots(ctx.fact_freq, d)):
            out.append({"date": d, "kind": "incomplete", "slots": n})
    review = facts.review_days(ctx.conn, ctx.code, ctx.fact_freq, first)
    out += [{"date": d, "kind": "pending_review"} for d in sorted(review)]
    return sorted(out, key=lambda x: x["date"])


def _token_parts(ctx) -> tuple:
    """令牌里与周期无关的部分：同一上下文内各周期相同，只取一次。"""
    deps = [[ds, facts.series_gens(ctx.conn, ctx.code, ds)[1]]
            for ds in ("day", ctx.fact_freq) if ds]
    gens = [[r["kind"], r["item"], r["binding_gen"]] for r in ctx.conn.execute(
        "SELECT kind, item, binding_gen FROM binding_state WHERE market=? ORDER BY kind, item",
        (ctx.market,))]
    chain = [ctx.chain.anchor, ctx.chain.today_confirmed] if ctx.chain else None
    # 前复权相邻性与周线归属都读日历：只改日历（补入开市日、推出休市日）也要让旧分页 409
    cal = hashlib.sha256(json.dumps([ctx.trading_days, ctx.closed_days]).encode()).hexdigest()
    return (deps, gens, chain, cal, facts.run_identity(ctx.conn),
            hk_vendor_qfq.cache_version(ctx.conn, ctx.code) if ctx.vendor else None)


def _token(ctx, freq) -> str:
    deps, gens, chain, cal, run_id, vendor = ctx.memo("token_parts", lambda: _token_parts(ctx))
    payload = [ctx.code, freq, ctx.adjust, config.READ_RULES_VERSION, deps, gens, ctx.today,
               chain, cal, run_id, vendor]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:24]


def _notices(ctx) -> list:
    out = []
    if ctx.chain and ctx.chain.stop_reason:
        out.append(_notice("qfq_from", ctx.chain.qfq_from))
    if ctx.chain and ctx.is_trading_today and not ctx.chain.today_confirmed:
        out.append(_notice("today_unconfirmed", ctx.chain.anchor))
    return out


def _has_facts(ctx) -> bool:
    """含已隔离的行：整段隔离后仍给空视图与当前令牌，旧令牌据此得到 409 而不是「无数据」。"""
    if ctx.day_rows:
        return True
    return any(ctx.conn.execute(f"SELECT 1 FROM {table} WHERE code=? LIMIT 1", (ctx.code,)).fetchone()
               for table in ("current_day_bars", "minute_bars"))


def _read(conn, code, freq, adjust, before, limit, now, ctx=None) -> View | None:
    if freq not in PERIODS:
        raise ValueError(f"unsupported freq: {freq}")
    ctx = ctx or _Ctx(conn, code, adjust, now)
    if not _has_facts(ctx) and not (freq in MINUTE_PERIODS and ctx.fact_freq is None):
        return None
    notices, bars, has_more, week_incomplete, vendor_meta = _notices(ctx), [], False, [], None
    label = _LABEL["vendor" if ctx.vendor else ctx.adjust]
    try:
        if freq in MINUTE_PERIODS and not ctx.fact_freq:
            raise periods.UnsupportedPeriod(ctx.market, freq)
        if ctx.vendor:
            bars, has_more, week_incomplete, vendor_meta = _vendor_bars(ctx, freq, before, limit)
        elif freq == "week":
            bars, has_more, week_incomplete = _week_bars(ctx, before, limit)
        elif freq == "day":
            bars = _day_bars(ctx)
            if before:
                bars = [b for b in bars if b["dt"] < before]
            has_more = len(bars) > limit
            bars = bars[-limit:]
        else:
            bars, has_more = _minute_bars(ctx, freq, before, limit)
    except periods.UnsupportedPeriod:
        notices = [_notice("unsupported")]
    if freq in MINUTE_PERIODS and bars:
        notices += _backfill_notices(ctx, len(bars), limit)
    if before is None and bars and len(bars) < limit and not has_more \
            and not any(n["code"] == "structure_short" for n in notices):
        notices.append(_notice("structure_short"))       # 首页未取够默认窗口（spec §1 非自选首开）
    incomplete = (week_incomplete if freq == "week"
                  else _incomplete_days(ctx, bars) if freq in MINUTE_PERIODS and bars else [])
    dataset = _stale_dataset(ctx, freq)
    stale, stale_age, _ = dataset_stale(conn, code, dataset, ctx.now)
    if not instance.is_demo() and vendor_meta and (vendor_meta["frozen"] or vendor_meta["stale"] or vendor_meta["lagging"]):
        stale = True                                     # 冻结、重取失败或未追上收盘：缓存停在旧版本
    served = {s for b in bars for s in (b.get("sources") or [])}
    source = (bars[-1].get("source") if bars else None) or bindings.active(
        conn, ctx.market, ctx.kind, FetchItem.DAY_HISTORY)[0]
    coverage = ({"qfq_from": ctx.chain.qfq_from, "qfq_through": ctx.chain.anchor,
                 "stop_reason": ctx.chain.stop_reason} if ctx.chain
                else {"qfq_from": None, "qfq_through": None, "stop_reason": None})
    if instance.is_demo():
        coverage["data_status"] = {"phase": "historical", "day": bars[-1]["dt"][:10] if bars else None, "at": None}
        notices = [n for n in notices if n["code"] not in ("today_unconfirmed", "backfill_pending")]
        notices = [{**n, "text": f"历史样本仅 {len(bars)} 根，少于 {limit} 根分析窗口；demo 不补齐"}
                   if n["code"] == "structure_short" else n for n in notices]
    else:
        coverage["data_status"] = _data_status(ctx, freq, vendor_meta)
    return View(code=code, freq=freq, adjust=ctx.adjust,
                bars=[{k: b[k] for k in _BAR_KEYS} for b in bars],
                token=_token(ctx, freq), adjust_label=label, source=source, coverage=coverage,
                notices=notices, incomplete_days=incomplete, has_more=has_more,
                oldest_dt=bars[0]["dt"] if bars else None, stale=stale, stale_age_s=stale_age,
                last_commit_at=facts.last_commit_at(conn, code, dataset) if dataset else None,
                degraded=bool(served & ctx.cold))


def read_view(conn, code, freq, *, adjust="qfq", before=None, limit=config.DEFAULT_WINDOW, now=None):
    with facts.read_txn(conn):
        return _read(conn, code, freq, adjust, before, limit, now)


def read_bundle(conn, code, freqs, *, adjust="qfq", now=None) -> dict:
    """各周期共用一个读取上下文（日线、日历、因子链与本次读取内的分钟行只取一次）；上下文随本次调用结束。"""
    with facts.read_txn(conn):
        out, ctx = {}, None
        for f in freqs:
            if ctx is None and f in PERIODS:
                ctx = _Ctx(conn, code, adjust, now)
            out[f] = _read(conn, code, f, adjust, None, config.DEFAULT_WINDOW, now, ctx)
        return out



def limit_ratio(code) -> str | None:
    """A 股涨跌停比例（十进制字符串）：科创板、创业板 20%，其余 10%（含主板风险警示股，沪深交易所已由 5% 调为
    10%）；指数与港股没有涨跌停。北交所与上市初期不设涨跌幅的新股不在范围（计划 B 执行期澄清）。"""
    if market_of(code) != "CN" or kind_of(code) == "index":
        return None
    return "0.20" if code.startswith(("sh68", "sz30")) else "0.10"


def limit_up_price(pc, ratio: str) -> float:
    """涨停价 = 前收 × (1 + 比例)，按分四舍五入（交易所规则；十进制半进位，避免二进制 round 在半分处少进一分）。"""
    price = Decimal(str(pc)) * (1 + Decimal(ratio))
    return float(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _reference_close(conn, market, day_rows, closed_days, today):
    """港股与指数的参考前收（计划 2026-09-29 D6）：最近一个已收盘交易日的收盘，只有它与今天之间的每个交易日都有
    可读日线行（停牌行也算）时才用；否则返回 None（落后的代码不用更早的收盘价算涨跌）。只要求日历已知的开市日：
    两个市场都有供应商年表，日历未知的工作日（年表缺失）照旧不置空，免得无日历时每个空档都把涨跌清掉。"""
    if not closed_days:
        return None
    have = {r["trade_date"] for r in day_rows}
    day, end = date.fromisoformat(closed_days[-1]["trade_date"]) + timedelta(days=1), date.fromisoformat(today)
    while day < end:
        if calendar.is_trading_day(conn, market, day.isoformat()) and day.isoformat() not in have:
            return None
        day += timedelta(days=1)
    return closed_days[-1]["close"]


def quote(conn, code, *, now=None) -> dict | None:
    """右栏与自选行情（spec §6.4）：价格与参考前收必须属于同一交易日。

    当日首根分钟出现前显示昨收并标「昨收」，涨跌为空；参考前收缺失时涨跌为空，不用昨收代替。
    停牌占位不作为最新价。港股与指数没有盘前参考价取数项，参考前收即上一交易日收盘：最近收盘与今天之间缺交易日的
    日线行时置空（见 _reference_close）。无任何事实返回 None。
    limit_up：参考前收已确认且价格为当日最新时，price >= 涨停价 − 1e-9（limit_ratio、limit_up_price）。
    """
    now = now or datetime.now()
    today = now.date().isoformat()
    market, kind = market_of(code), kind_of(code)
    fact = bindings.binding(market, kind, FetchItem.MINUTE_HISTORY).minute_fact_freq
    with facts.read_txn(conn):
        day_rows = facts.read_day_rows(conn, code)
        minutes = [r for r in facts.read_minute_rows(conn, code, fact, f"{today} 00:00", f"{today} 23:59")
                   if r["trade_state"] != "suspended"]
        closed_days = [r for r in day_rows if r["provenance"] in ("live", "final") and r["sf"] == 0
                       and r["close"] is not None and r["trade_date"] < today]
        if not minutes and not closed_days:
            return None
        stale, _, _ = dataset_stale(conn, code, fact or "day", now)
        prev_close = _reference_close(conn, market, day_rows, closed_days, today)
    if not minutes:
        last = closed_days[-1]
        return {"price": last["close"], "price_time": None, "price_label": "昨收", "pc": None,
                "pct": None, "limit_up": False, "trade_date": last["trade_date"], "stale": stale}
    latest = minutes[-1]
    if market == "HK" or kind == "index":
        pc = prev_close
    else:
        today_row = next((r for r in reversed(day_rows) if r["trade_date"] == today), None)
        pc = today_row["pc"] if today_row is not None else None
    pct = round((latest["close"] / pc - 1) * 100, 2) if pc else None
    ratio = limit_ratio(code)
    limit_up = bool(pc and ratio and latest["close"] >= limit_up_price(pc, ratio) - 1e-9)
    return {"price": latest["close"], "price_time": latest["slot_end"], "price_label": "最新",
            "pc": pc, "pct": pct, "limit_up": limit_up, "trade_date": today, "stale": stale}
