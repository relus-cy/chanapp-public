"""取数 schema 的原始行契约与词汇。

接入语义由 ingest 与 admission 共同执行。
provider 只输出这里的行类型；复权、聚合、令牌都在读时计算。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum


class FetchItem(str, Enum):
    DAY_HISTORY = "day_history"
    MINUTE_HISTORY = "minute_history"
    MINUTE_LIVE = "minute_live"
    PREOPEN_REF = "preopen_ref"
    SESSION_CALENDAR = "session_calendar"
    INSTRUMENT_LIST = "instrument_list"


PROVENANCE_RANK = {"preopen": 0, "live": 1, "final": 2}
BAR_STATES = ("forming", "closed")
TRADE_STATES = ("traded", "no_trade", "suspended")
SHARES_PER_UNIT = {"lot": 100, "share": 1}


def new_batch_id() -> str:
    return uuid.uuid4().hex


def market_of(code: str) -> str:
    if code.startswith("hk"):
        return "HK"
    if code[:2] in ("sh", "sz"):
        return "CN"
    raise ValueError(f"unsupported code: {code}")


def kind_of(code: str) -> str:
    market_of(code)
    return "index" if code.startswith(("sh000", "sz399")) else "stock"


def to_shares(volume: float | None, unit: str) -> float | None:
    factor = SHARES_PER_UNIT[unit]
    return None if volume is None else volume * factor


@dataclass(frozen=True)
class RawDayRow:
    code: str
    trade_date: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    volume_unit: str
    amount: float | None
    currency: str
    pc: float | None
    sf: int
    provenance: str
    batch_id: str


@dataclass(frozen=True)
class RawMinuteRow:
    code: str
    trade_date: str
    slot_end: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    volume_unit: str
    amount: float | None
    state: str
    trade_state: str
    batch_id: str


@dataclass(frozen=True)
class CalendarRow:
    market: str
    date: str
    is_open: bool
    sessions: tuple = ()


@dataclass(frozen=True)
class InstrumentRow:
    code: str
    name: str
    list_date: str | None
    delist_date: str | None
    kind: str
