"""港股供应商前复权缓存（spec §7 方案 (b)；计划 A 的 A4）。

不进事实表：每个 (标的, 周期) 只有一个已发布版本，整段原子替换，旧版本保留供审计。
- 只缓存已收盘 bar：closed_through（最近已收盘交易日，含）之后的 bar 发布时丢弃；
- 重取判定只比较已收盘、标签相同的重叠 bar，盘中 forming 不触发；新窗口与缓存没有重叠（缓存久未刷新）
  时也整段重取，否则追加会留断档并混用复权基准；
- 保留最近 KEEP_VERSIONS 个版本供审计，更早的版本在发布时清理；空序列不发布；
- 发布前整段校验（重复时间标签、非有限值、非正价格、OHLC 越界、负量），任何一根不合法则整段不发布，
  已发布版本不变（raw 行合法不能证明供应商复权序列合法）；
- 冻结（手动切到冷备期间）后拒绝发布，读侧只服务到冻结点并标 stale；切到冷备时由 bindings.switch 在同一事务冻结，
  切回主源不解冻，直到两个周期整段重取成功、在同一写事务里发布时才解冻；
- 采集器用 publish_set 在同一写事务里同时发布 day 与 m30：先复核取数时的绑定代次与取数前的缓存版本
  （期间已有更新版本发布则丢弃迟到候选），任一周期不合法则两个都不发布，避免 bundle 读到两个复权基准；
- 追加模式同样先整段校验取回的候选（含与缓存重叠的部分），任一周期取回为空视为不合法；
- 重取失败保留旧版并标 stale（当天定稿已发布后只取历史区间、又没有重述证据的刷新除外，规则见采集器
  refresh_vendor_qfq）；读取继承 raw 事实的隔离（已证实错误的日期或槽位不服务）；
- 缓存周期为 day 与 m30；m60 由缓存 m30 聚合、周线由缓存 day 聚合，都在视图层完成。
"""
from __future__ import annotations

from chanapp.engine.kline import admission, facts
from chanapp.engine.kline.rows import to_shares

FREQS = ("day", "m30")
PRICE_TOL = 0.0015          # 与事实层港股核对容差一致
KEEP_VERSIONS = 5


class VendorFrozen(RuntimeError):
    """缓存已冻结（冷备期间），不接受新版本。"""


class VendorSuperseded(RuntimeError):
    """取数期间已有更新版本发布（同一绑定代次下的迟到候选），整组丢弃。"""


def _dt(bar, freq) -> str:
    return bar["trade_date"] if freq == "day" else bar["slot_end"]


def _closed(bars, freq, closed_through) -> list:
    return sorted((b for b in bars if b["trade_date"] <= closed_through), key=lambda b: _dt(b, freq))


def _validate(code, freq, rows) -> None:
    seen = set()
    for b in rows:
        dt = _dt(b, freq)
        problem = ("duplicate_key" if dt in seen
                   else admission.price_problem(b["open"], b["high"], b["low"], b["close"])
                   or admission.quantity_problem(b["volume"], allow_none=False))
        if problem:
            raise ValueError(f"{code} {freq} 供应商前复权序列不合法（{dt}: {problem}），不发布")
        seen.add(dt)


def _meta(conn, code, freq):
    return conn.execute("SELECT version, fetched_at, frozen, stale FROM vendor_qfq_publish"
                        " WHERE code=? AND freq=?", (code, freq)).fetchone()


def _prepare(code, freq, bars, closed_through) -> list:
    if freq not in FREQS:
        raise ValueError(freq)
    rows = _closed(bars, freq, closed_through)
    if not rows:
        raise ValueError(f"{code} {freq} 供应商前复权序列为空，不发布")
    _validate(code, freq, rows)
    return rows


def _insert_version(conn, code, freq, rows, *, stale=False) -> int:
    """写一个新版本并切换指针；调用方持有写事务。stale 为新版本的 stale 标记（缺省清除）。"""
    meta = _meta(conn, code, freq)
    version = (meta["version"] if meta else 0) + 1
    conn.executemany(
        "INSERT INTO vendor_qfq_bars(code, freq, version, dt, open, high, low, close, volume)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(code, freq, version, _dt(b, freq), b["open"], b["high"], b["low"], b["close"],
          to_shares(b["volume"], b.get("volume_unit", "share"))) for b in rows])
    conn.execute("INSERT INTO vendor_qfq_publish(code, freq, version, fetched_at, frozen, stale)"
                 " VALUES (?,?,?,?,0,?) ON CONFLICT(code, freq) DO UPDATE SET"
                 " version=excluded.version, fetched_at=excluded.fetched_at, stale=excluded.stale",
                 (code, freq, version, facts.now_iso(), int(stale)))
    conn.execute("DELETE FROM vendor_qfq_bars WHERE code=? AND freq=? AND version<=?",
                 (code, freq, version - KEEP_VERSIONS))
    return version


def publish(conn, code, freq, bars, *, closed_through) -> int:
    """整段发布一个周期的新版本并切换指针（同一写事务）；返回新版本号。"""
    rows = _prepare(code, freq, bars, closed_through)
    with facts.write_txn(conn):
        meta = _meta(conn, code, freq)
        if meta is not None and meta["frozen"]:
            raise VendorFrozen(f"{code} {freq} 供应商前复权缓存已冻结")
        return _insert_version(conn, code, freq, rows)


def publish_set(conn, code, series: dict, *, closed_through, full: bool, binding_gens=None,
                unfreeze=False, expected_versions=None, keep_tail=False, freqs=FREQS) -> dict:
    """在同一写事务里发布 day 与 m30（spec §7 方案 (b) 规则 2、6）。

    freqs：通常为两个周期；只日线实例显式传 ("day",)，停用的分钟缓存不参与发布或解冻。
    full：两个周期都整段替换；否则各自只追加缓存末端之后的新收盘 bar（没有新 bar 的周期不出新版本，只清 stale）。
    binding_gens：取数时的港股个股绑定代次 {item: gen}，事务内复核，变化即抛 facts.StaleBinding。
    unfreeze：冻结中的缓存只在两个周期都重取成功时随发布一起解冻；否则冻结拒绝发布。
    expected_versions：取数前记下的已发布版本 {freq: version 或 None}，事务内复核，变化即抛 VendorSuperseded。
    keep_tail：整段替换时保留已发布版本里 closed_through 之后的 bar（只取历史区间的刷新不撤掉当天已发布的尾部；
    前复权以最新价为基准，历史重述不改变尾部）；新版本沿用原 stale（没重取的尾部不因此被认证为健康），
    不与 unfreeze 同用。
    任一周期为空或不合法时抛 ValueError，两个周期都不变。返回 {freq: 新版本号或 None}。"""
    if freqs not in (FREQS, ("day",)):
        raise ValueError("供应商缓存只允许日线或日线与分钟成套发布")
    with facts.write_txn(conn):
        for item, gen in (binding_gens or {}).items():
            current = facts.binding_gen(conn, "HK", "stock", item)
            if current != gen:
                raise facts.StaleBinding(f"HK/stock/{item}: fetched gen {gen} != current {current}")
        metas = {freq: _meta(conn, code, freq) for freq in freqs}
        for freq, version in (expected_versions or {}).items():
            current = metas[freq]["version"] if metas[freq] is not None else None
            if current != version:
                raise VendorSuperseded(f"{code} {freq}: fetched against version {version}, current {current}")
        if not unfreeze and any(m is not None and m["frozen"] for m in metas.values()):
            raise VendorFrozen(f"{code} 供应商前复权缓存已冻结")
        if keep_tail and unfreeze:
            raise ValueError(f"{code} 保留尾部的发布不能解冻")
        prepared = {}
        for freq in freqs:
            rows = (_prepare(code, freq, series[freq], closed_through) if full
                    else _extension(conn, code, freq, series[freq], closed_through=closed_through))
            if full and keep_tail:
                rows = rows + [_as_row(dt, r, freq) for dt, r in _published(conn, code, freq).items()
                               if dt[:10] > closed_through]
            if rows is not None:
                _validate(code, freq, rows)
            prepared[freq] = rows
        out = {}
        for freq, rows in prepared.items():
            if rows is None:
                conn.execute("UPDATE vendor_qfq_publish SET stale=0 WHERE code=? AND freq=?", (code, freq))
                out[freq] = None
            else:
                out[freq] = _insert_version(conn, code, freq, rows,
                                            stale=bool(metas[freq]["stale"]) if keep_tail and metas[freq] else False)
        if unfreeze:
            for freq in freqs:
                conn.execute("UPDATE vendor_qfq_publish SET frozen=0 WHERE code=? AND freq=?", (code, freq))
    return out


def _published(conn, code, freq) -> dict:
    meta = _meta(conn, code, freq)
    if meta is None:
        return {}
    return {r["dt"]: r for r in conn.execute(
        "SELECT dt, open, high, low, close, volume FROM vendor_qfq_bars WHERE code=? AND freq=?"
        " AND version=? ORDER BY dt", (code, freq, meta["version"]))}


def needs_refetch(conn, code, freq, fresh, *, closed_through) -> bool:
    """需要整段重取：新取回的序列与已发布版本出现重述（price_conflict），或两者没有重叠时为真。"""
    cached = _published(conn, code, freq)
    closed = _closed(fresh, freq, closed_through)
    if cached and closed and not any(_dt(b, freq) in cached for b in closed):
        return True
    return _conflict(cached, closed, freq)


def price_conflict(conn, code, freq, fresh, *, closed_through) -> bool:
    """已证实重述：新取回的序列与已发布版本在已收盘重叠 bar 上价格不同（超出容差）。没有重叠不算——它只说明
    要整段重取，不否定已发布的数据。"""
    return _conflict(_published(conn, code, freq), _closed(fresh, freq, closed_through), freq)


def _conflict(cached, closed, freq) -> bool:
    for b in closed:
        old = cached.get(_dt(b, freq))
        if old is not None and any(abs(old[k] - b[k]) > PRICE_TOL for k in ("open", "high", "low", "close")):
            return True
    return False


def _extension(conn, code, freq, fresh, *, closed_through) -> list | None:
    """缓存加上其末端之后新收盘的 bar；没有新 bar 返回 None。

    先对取回的已收盘候选整段校验（含与缓存重叠的部分）；候选为空视为不合法，与全量模式一致抛 ValueError。"""
    closed = _closed(fresh, freq, closed_through)
    if not closed:
        raise ValueError(f"{code} {freq} 供应商前复权追加候选为空，不发布")
    _validate(code, freq, closed)
    cached = _published(conn, code, freq)
    last = max(cached) if cached else ""
    new = [b for b in closed if _dt(b, freq) > last]
    if not new:
        return None
    return [_as_row(dt, r, freq) for dt, r in cached.items()] + new


def _as_row(dt, r, freq) -> dict:
    """已发布的一根 bar 还原成候选行（量已是股）。"""
    return {"trade_date": dt[:10], ("trade_date" if freq == "day" else "slot_end"): dt,
            **{k: r[k] for k in ("open", "high", "low", "close", "volume")}}


def extend(conn, code, freq, fresh, *, closed_through) -> int | None:
    """无重述时把缓存之后新收盘的 bar 追加为新版本（仍是整段发布）；无新 bar 返回 None。"""
    rows = _extension(conn, code, freq, fresh, closed_through=closed_through)
    return None if rows is None else publish(conn, code, freq, rows, closed_through=closed_through)


def freeze_rows(conn, code=None) -> None:
    """冻结（不开事务，供 bindings.switch 在切换事务内调用）。"""
    if code is None:
        conn.execute("UPDATE vendor_qfq_publish SET frozen=1")
    else:
        conn.execute("UPDATE vendor_qfq_publish SET frozen=1 WHERE code=?", (code,))


def freeze(conn, code=None) -> None:
    with facts.write_txn(conn):
        freeze_rows(conn, code)


def unfreeze(conn, code=None) -> None:
    with facts.write_txn(conn):
        if code is None:
            conn.execute("UPDATE vendor_qfq_publish SET frozen=0")
        else:
            conn.execute("UPDATE vendor_qfq_publish SET frozen=0 WHERE code=?", (code,))


def mark_stale(conn, code, freq=None) -> None:
    """标 stale；freq 缺省时两个周期一起标（一次写事务）。"""
    with facts.write_txn(conn):
        if freq is None:
            conn.execute("UPDATE vendor_qfq_publish SET stale=1 WHERE code=?", (code,))
        else:
            conn.execute("UPDATE vendor_qfq_publish SET stale=1 WHERE code=? AND freq=?", (code, freq))


def mark_stale_if(conn, code, versions) -> bool:
    """两个周期的已发布版本仍是 versions 时一起标 stale（一次写事务）；期间已有新版本发布则不标，返回 False。
    用于补落盘迟到的重述证据：证据只否定取数时的那个版本。"""
    with facts.write_txn(conn):
        for freq, version in versions.items():
            meta = _meta(conn, code, freq)
            if (meta["version"] if meta else None) != version:
                return False
        conn.execute("UPDATE vendor_qfq_publish SET stale=1 WHERE code=?", (code,))
    return True


def read(conn, code, freq) -> tuple:
    """(bars, meta)；未发布过返回 ([], None)。bars 带 dt 与 trade_date，量为股。"""
    meta = _meta(conn, code, freq)
    if meta is None:
        return [], None
    hidden_days = facts.quarantined_keys(conn, code, "day")
    hidden = hidden_days if freq == "day" else facts.quarantined_keys(conn, code, freq) | hidden_days
    bars = [{"dt": dt, "trade_date": dt[:10], **{k: r[k] for k in ("open", "high", "low", "close", "volume")}}
            for dt, r in _published(conn, code, freq).items()
            if dt not in hidden and dt[:10] not in hidden]
    return bars, {"version": meta["version"], "fetched_at": meta["fetched_at"],
                  "frozen": bool(meta["frozen"]), "stale": bool(meta["stale"])}


def cache_version(conn, code) -> list:
    """进入视图令牌的缓存身份：各周期的已发布版本与冻结、stale 状态。"""
    return [[r["freq"], r["version"], r["frozen"], r["stale"]] for r in conn.execute(
        "SELECT freq, version, frozen, stale FROM vendor_qfq_publish WHERE code=? ORDER BY freq", (code,))]
