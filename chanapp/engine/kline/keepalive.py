"""冷备在线能力保活（spec §8、§13；计划 A 决定 8）：每周六由采集器调度。

- 对每个有冷备的历史类绑定，抽 1 个标的 × 近 5 个交易日，用冷备取数，与事实层已有的主源行比对；
- 只比对、不入库：结论经 bindings.record_probe 写回 probe_runs（probe_id = keepalive-<主探针>）；
- 只看到连通或空返回不算通过：必须取回并比对通过至少一行；事实层没有主源行可比时不记录；
- 参照只取主源写入的行：手动切到冷备期间由冷备入库的行不作参照；
- 比对口径：OHLC 差值在事实层核对容差内（A 股个股逐分、A 股指数 0.01 舍入边界、港股 0.0015）；
  个股成交量换算成股后相对误差 ≤ 1%；
  指数成交量各源口径不一（实测约 4.5 倍），不比；
- 盘中增量（MINUTE_LIVE）周六无法取数，不在保活范围；日历与标的列表形态不同，由 selfcheck 另行覆盖。
"""
from __future__ import annotations

import logging
from contextlib import nullcontext

from chanapp.engine.kline import bindings, facts
from chanapp.engine.kline.rows import FetchItem, to_shares

log = logging.getLogger(__name__)

CRITERIA_VERSION = "keepalive-1"
SAMPLE_DAYS = 5
_ITEMS = (FetchItem.DAY_HISTORY, FetchItem.MINUTE_HISTORY)
_SAMPLES = {("CN", "stock"): "sh600036", ("CN", "index"): "sh000001"}
_CLOSE_SLOT = {"CN": "15:00", "HK": "16:00"}


_PRICES = ("open", "high", "low", "close")


def _as_dict(row) -> dict:
    return row if isinstance(row, dict) else {k: getattr(row, k) for k in
                                              ("open", "high", "low", "close", "volume", "volume_unit")}


def _match(primary, cold, kind, market) -> bool:
    cold = _as_dict(cold)
    tol = facts.PRICE_TOL.get((market, kind), 0.0015)
    for f in _PRICES:
        if (primary[f] is None) != (cold[f] is None):
            return False
        if primary[f] is not None and abs(primary[f] - cold[f]) > tol:
            return False
    if kind == "index":
        return True                                   # 指数成交量各源口径不一，不比
    p_vol = to_shares(primary["volume"], primary["volume_unit"])
    c_vol = to_shares(cold["volume"], cold["volume_unit"])
    if p_vol is None or c_vol is None:
        return p_vol == c_vol
    return abs(p_vol - c_vol) <= 0.01 * max(abs(p_vol), 1.0)


def _primary_rows(conn, code, item, fact, before, primary):
    """事实层近 SAMPLE_DAYS 个交易日由主源写入的定稿行，键为日期或槽位。"""
    days = [r for r in facts.read_day_rows(conn, code, end=before)
            if r["provenance"] == "final" and not r["sf"] and r["trade_date"] < before and r["source"] == primary]
    days = days[-SAMPLE_DAYS:]
    if not days:
        return None, {}
    start, end = days[0]["trade_date"], days[-1]["trade_date"]
    if item == FetchItem.DAY_HISTORY:
        return (start, end), {r["trade_date"]: r for r in days}
    rows = facts.read_minute_rows(conn, code, fact, f"{start} 00:00", f"{end} 23:59")
    keyed = {r["slot_end"]: r for r in rows
             if r["state"] == "closed" and r["trade_state"] == "traded" and r["source"] == primary}
    return (start, end), keyed


def _probe(conn, provider, b, code, now) -> dict | None:
    span, primary = _primary_rows(conn, code, b.item, b.minute_fact_freq, now.date().isoformat(), b.primary)
    if not primary:
        return None
    start, end = span
    try:
        if b.item == FetchItem.DAY_HISTORY:
            rows = provider.day_history(code, start, end)
            cold = {r.trade_date: r for r in rows}
        else:
            rows = provider.minute_history(code, b.minute_fact_freq, f"{start} 09:30",
                                           f"{end} {_CLOSE_SLOT[b.market]}", now=now)
            cold = {r.slot_end: r for r in rows if r.trade_state == "traded"}
        error = None
    except Exception as exc:  # noqa: BLE001 — 冷备任何失败都是 fail 结论，不向调度抛出
        cold, error = {}, f"{type(exc).__name__}: {exc}"
    compared = sum(1 for k in primary if k in cold)
    mismatched = [k for k in primary if k in cold and not _match(primary[k], cold[k], b.kind, b.market)]
    missing = [k for k in primary if k not in cold]
    ok = error is None and compared > 0 and not mismatched and not missing
    return {"probe_id": f"keepalive-{b.probe_id}", "market": b.market, "kind": b.kind,
            "item": b.item.value, "source": b.cold, "code": code, "verdict": "pass" if ok else "fail",
            "compared": compared, "mismatched": mismatched[:5], "missing": len(missing), "error": error,
            "span": [start, end]}


def run(conn, providers, *, now, hk_code=None, writer=None, minute_enabled=None) -> list:
    """providers：源名 → provider 的可调用；writer：写者锁上下文工厂（采集器传入，锁外取数、锁内记录）。"""
    samples = dict(_SAMPLES)
    if hk_code:
        samples[("HK", "stock")] = hk_code
    results = []
    for b in bindings.BINDINGS:
        code = samples.get((b.market, b.kind))
        if b.cold is None or b.item not in _ITEMS or code is None:
            continue
        if b.item == FetchItem.MINUTE_HISTORY and minute_enabled is not None and not minute_enabled(code):
            continue
        try:
            provider = providers(b.cold)
        except Exception as exc:  # noqa: BLE001 — 冷备构造失败同样是 fail 结论
            provider, init_error = None, f"{type(exc).__name__}: {exc}"
        if provider is None:
            span, primary = _primary_rows(conn, code, b.item, b.minute_fact_freq, now.date().isoformat(), b.primary)
            if not primary:
                continue
            result = {"probe_id": f"keepalive-{b.probe_id}", "market": b.market, "kind": b.kind,
                      "item": b.item.value, "source": b.cold, "code": code, "verdict": "fail",
                      "compared": 0, "mismatched": [], "missing": len(primary), "error": init_error,
                      "span": list(span)}
        else:
            if b.item == FetchItem.MINUTE_HISTORY and minute_enabled is not None and not minute_enabled(code):
                continue
            result = _probe(conn, provider, b, code, now)
        if result is None:
            continue
        with (writer() if writer else nullcontext()):
            bindings.record_probe(conn, result["probe_id"], b.market, b.kind, b.item, b.cold,
                                  result["verdict"],
                                  {"endpoint": b.item.value, "sample": f"1x1x{SAMPLE_DAYS}",
                                   "criteria_version": CRITERIA_VERSION, "code": code,
                                   "compared": result["compared"], "error": result["error"]})
        if result["verdict"] == "fail":
            log.warning("冷备保活未通过 %s/%s/%s %s", b.market, b.kind, b.item.value, b.cold)
        results.append(result)
    return results
