"""交易时段判断：经 GET /api/session 决定前端是否自动刷新、状态栏是否显示「交易中」。

- cn：09:25–15:05（含边界，覆盖集合竞价到收盘缓冲）
- hk：09:30–16:15（含边界，覆盖采集器港股盘中窗口的收盘后定格分钟 16:11 并留缓冲）
时段须覆盖 K 线采集器的盘中取数窗口，否则采集器还在取盘中数据时页面已停刷。
只看交易日真假，不读半日市会话：港股半日市下午仍判为交易中。
now 为东八区本地时间（naive；/api/session 按 Asia/Shanghai 取，不依赖服务器时区）。

交易日判断：K 线门面可注册交易日历钩子 fn(market, date) -> True/False/None。
钩子判定休市（False）时不在交易时段；钩子缺失、未覆盖该日（None）或出错时，
回落到周一至周五规则（公开演示包即此行为）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Callable

log = logging.getLogger(__name__)

_SESSIONS = {
    "cn": (time(9, 25), time(15, 5)),
    "hk": (time(9, 30), time(16, 15)),
}
_calendar_hook: Callable[[str, date], bool | None] | None = None


def set_calendar_hook(fn: Callable[[str, date], bool | None] | None) -> None:
    """注册（或以 None 清除）交易日历钩子。"""
    global _calendar_hook
    _calendar_hook = fn


def _is_trading_day(day: date, market: str) -> bool:
    hook = _calendar_hook
    if hook is not None:
        try:
            verdict = hook(market, day)
        except Exception:  # noqa: BLE001 — 日历不可用时回落到工作日规则
            log.warning("交易日历钩子失败 market=%s day=%s", market, day, exc_info=True)
            verdict = None
        if verdict is not None:
            return bool(verdict)
    return day.weekday() < 5


def is_session_open(now: datetime, market: str) -> bool:
    """now（本地时间）是否处于 market（"cn"/"hk"）交易时段内。"""
    if market not in _SESSIONS:
        raise ValueError(f"unsupported market: {market}")
    if not _is_trading_day(now.date(), market):
        return False
    start, end = _SESSIONS[market]
    return start <= now.time() <= end
