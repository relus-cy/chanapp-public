"""交易时段判断：决定前端是否自动刷新行情。

- cn：周一至五 09:25–15:05（含边界，覆盖集合竞价到收盘缓冲）
- hk：周一至五 09:30–16:05
now 为本地时间（+08:00，服务器/浏览器均在东八区口径）；节假日不特殊处理
（节假日开市时段无新数据，刷新命中缓存 TTL，代价可接受）。
"""
from __future__ import annotations

from datetime import datetime, time

_SESSIONS = {
    "cn": (time(9, 25), time(15, 5)),
    "hk": (time(9, 30), time(16, 5)),
}


def is_session_open(now: datetime, market: str) -> bool:
    """now（本地时间）是否处于 market（"cn"/"hk"）交易时段内。"""
    if market not in _SESSIONS:
        raise ValueError(f"unsupported market: {market}")
    if now.weekday() >= 5:  # 周六/周日
        return False
    start, end = _SESSIONS[market]
    return start <= now.time() <= end
