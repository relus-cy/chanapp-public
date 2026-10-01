"""市场会话与周期槽位（区间末端标签）。设计 §5.2、§6.2。

槽位按会话生成：每个会话从开始时刻起按步长给末端标签，会话末尾不足一个步长的
短桶保留（港股上午 11:30–12:00 的 m60 短桶）。集合竞价归属待探针 P3/PH1，
在此之前 09:30 这类开盘前标签不在任何网格里（准入按 off_grid 拒绝）。
"""
from __future__ import annotations

from datetime import date, timedelta

SESSIONS = {
    "CN": (("09:30", "11:30"), ("13:00", "15:00")),
    "HK": (("09:30", "12:00"), ("13:00", "16:00")),
}
FREQ_MINUTES = {"m5": 5, "m15": 15, "m30": 30, "m60": 60}


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def _label(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


def _grid(sessions, step: int) -> tuple:
    out = []
    for start, end in sessions:
        begin, finish = _minutes(start), _minutes(end)
        cursor = begin + step
        while cursor < finish:
            out.append(_label(cursor))
            cursor += step
        out.append(_label(finish))
    return tuple(out)


_SLOTS = {(market, freq): _grid(sessions, minutes)
          for market, sessions in SESSIONS.items()
          for freq, minutes in FREQ_MINUTES.items()}


def slots(market: str, freq: str) -> tuple:
    return _SLOTS[(market, freq)]


def bucket_end(market: str, freq: str, hhmm: str) -> str | None:
    """hhmm 所在会话内，第一个不早于它的 freq 槽位；不在任何会话内返回 None。"""
    t = _minutes(hhmm)
    for start, end in SESSIONS[market]:
        begin, finish = _minutes(start), _minutes(end)
        if begin < t <= finish:
            for label in _SLOTS[(market, freq)]:
                m = _minutes(label)
                if begin < m <= finish and m >= t:
                    return label
    return None


def week_start(date_str: str) -> str:
    day = date.fromisoformat(date_str)
    return (day - timedelta(days=day.weekday())).isoformat()
