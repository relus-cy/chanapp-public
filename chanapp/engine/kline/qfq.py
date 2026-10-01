"""等比前复权因子链（spec §6.1）：因子是已存原始日线的纯函数，不存表、无状态机。

multipliers[d] = Π(除权日 e 的因子)，e 取 d < e <= anchor；覆盖范围从锚点向前延伸，
遇到断点（交易日缺行、pc 缺失、因子非法、日历未覆盖）即停。全精度计算，只在显示时舍入。
两行之间的工作日必须是已知开市（且该股停牌）或已知休市；既不在开市日也不在休市日的工作日是未知，
不能当作相邻。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class QfqChain:
    anchor: str | None
    qfq_from: str | None
    multipliers: dict
    stop_reason: str | None
    today_confirmed: bool


def _v(row, key):
    return row[key]


def _link(prev, cur, adjacent):
    """返回 (factor, reason)：factor 非 None 表示两行可连。"""
    ok = adjacent(_v(prev, "trade_date"), _v(cur, "trade_date"))
    if ok is None:
        return None, "unknown_calendar"
    if not ok:
        return None, "gap"
    pc, close = _v(cur, "pc"), _v(prev, "close")
    if pc is None or not math.isfinite(pc) or pc <= 0:
        return None, "missing_pc"
    factor = pc / close if close else math.inf
    if not math.isfinite(factor) or factor <= 0:
        return None, "bad_factor"
    return factor, None


def build_chain(day_rows, trading_days, *, today, closed_days=frozenset()) -> QfqChain:
    rows = sorted(day_rows, key=lambda r: _v(r, "trade_date"))
    valid = [r for r in rows if _v(r, "provenance") in ("live", "final") and _v(r, "sf") == 0
             and _v(r, "close") is not None]
    suspended = {_v(r, "trade_date") for r in rows
                 if _v(r, "sf") == 1 and _v(r, "provenance") != "preopen"}
    position = {d: i for i, d in enumerate(trading_days)}
    closed = set(closed_days)

    def adjacent(a, b):
        if a not in position or b not in position:
            return None
        day = date.fromisoformat(a) + timedelta(days=1)
        while day.isoformat() < b:
            iso = day.isoformat()
            if day.weekday() < 5 and iso not in position and iso not in closed:
                return None
            day += timedelta(days=1)
        return all(d in suspended for d in trading_days[position[a] + 1:position[b]])

    chain = [(_v(r, "trade_date"), r) for r in valid]
    links = [_link(prev, cur, adjacent) for (_, prev), (_, cur) in zip(chain, chain[1:])]
    preopen = next((r for r in rows if _v(r, "trade_date") == today
                    and _v(r, "provenance") == "preopen"), None)
    if preopen is not None and chain and chain[-1][0] != today:
        factor, reason = _link(chain[-1][1], preopen, adjacent)
        if factor is not None:
            chain.append((today, preopen))
            links.append((factor, None))
    if not chain:
        return QfqChain(None, None, {}, None, False)
    anchor = chain[-1][0]
    multipliers, cum = {}, 1.0
    qfq_from, stop = None, None
    for k in range(len(chain) - 1, -1, -1):
        trade_date, _ = chain[k]
        multipliers[trade_date] = cum
        qfq_from = trade_date
        if k == 0:
            break
        factor, reason = links[k - 1]
        if factor is None:
            stop = reason
            break
        cum *= factor
        if not math.isfinite(cum) or cum <= 0:
            stop = "bad_factor"
            break
    return QfqChain(anchor, qfq_from, multipliers, stop, anchor == today)
