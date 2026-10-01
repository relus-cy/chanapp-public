"""周期派生纯函数（spec §6.2）：按市场会话钟点桶聚合分钟事实，生成当日日线与周线。"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from chanapp.engine.kline import sessions
from chanapp.engine.kline.rows import to_shares


class UnsupportedPeriod(ValueError):
    def __init__(self, market, freq):
        super().__init__(f"该市场暂不提供 {freq}（{market}）")


def _source_of(row):
    # 热路径：sqlite3.Row 与 dict 直接取，不为每行生成 keys 列表；其余类型照旧
    if type(row) is sqlite3.Row:
        try:
            return row["source"]
        except IndexError:
            return None
    if type(row) is dict:
        return row.get("source")
    keys = row.keys() if hasattr(row, "keys") else ()
    return row["source"] if "source" in keys else None


def _bar(group, dt, trade_date) -> dict:
    traded = [r for r in group if r["volume"] > 0]
    priced = traded or [group[-1]]
    amounts = [r["amount"] for r in group]
    sources = [s for s in (_source_of(r) for r in group) if s]
    return {
        "dt": dt, "trade_date": trade_date,
        "open": priced[0]["open"], "high": max(r["high"] for r in priced),
        "low": min(r["low"] for r in priced), "close": priced[-1]["close"],
        "volume": sum(to_shares(r["volume"], r["volume_unit"]) for r in group),
        "amount": None if any(a is None for a in amounts) else sum(amounts),
        "forming": any(r["state"] == "forming" for r in group),
        "slots": len(group),
        # 来源按批次：聚合 bar 取最后一行贡献事实的来源，另记全部贡献来源（计划 B 门面契约）
        "source": sources[-1] if sources else None,
        "sources": sorted(set(sources)),
    }


def aggregate_minutes(rows, *, market, fact_freq, target_freq) -> list:
    fact, target = sessions.FREQ_MINUTES[fact_freq], sessions.FREQ_MINUTES[target_freq]
    if target < fact or target % fact:
        raise UnsupportedPeriod(market, target_freq)
    groups: dict = {}
    for r in rows:
        if r["trade_state"] == "suspended":
            continue
        hhmm = r["slot_end"][11:]
        label = hhmm if target == fact else sessions.bucket_end(market, target_freq, hhmm)
        if label is None:
            continue
        groups.setdefault((r["trade_date"], label), []).append(r)
    return [_bar(group, f"{day} {label}", day) for (day, label), group in groups.items()]


def day_bar_from_minutes(rows, *, trade_date, forming) -> dict | None:
    live = [r for r in rows if r["trade_state"] != "suspended"]
    if not live:
        return None
    bar = _bar(live, trade_date, trade_date)
    bar["forming"] = forming or bar["forming"]
    return bar


def week_bars(day_bars, *, trading_days, suspended, today) -> tuple:
    """周线（spec §6.2）：桶键为稳定的周一，标签为该周最后一个有 bar 的交易日。

    输入是已换到同一锚点的日线 bar（不含 preopen、不含停牌日）；整周停牌不出 bar；
    缺失交易日与停牌分开，日历未覆盖的周标 unknown_calendar；当前周 forming。
    """
    groups: dict = {}
    for bar in sorted(day_bars, key=lambda b: b["dt"]):
        groups.setdefault(sessions.week_start(bar["dt"]), []).append(bar)
    out, incomplete = [], []
    for start, group in groups.items():
        end = (date.fromisoformat(start) + timedelta(days=6)).isoformat()
        expected = [d for d in trading_days if start <= d <= end]
        present = {b["dt"] for b in group}
        if not expected:
            incomplete.append({"week": start, "reason": "unknown_calendar"})
        else:
            missing = [d for d in expected if d <= today and d not in present and d not in suspended]
            if missing:
                incomplete.append({"week": start, "missing": missing})
        amounts = [b.get("amount") for b in group]
        sources = [s for b in group for s in (b.get("sources") or [])]
        out.append({
            "dt": group[-1]["dt"], "week_start": start,
            "open": group[0]["open"], "high": max(b["high"] for b in group),
            "low": min(b["low"] for b in group), "close": group[-1]["close"],
            "volume": sum(b["volume"] for b in group),
            "amount": None if any(a is None for a in amounts) else sum(amounts),
            "forming": bool(group[-1].get("forming")) or bool(expected and expected[-1] > today),
            "trade_date": group[-1]["dt"], "source": group[-1].get("source"),
            "sources": sorted(set(sources)),
        })
    return out, incomplete
