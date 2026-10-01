"""准入纯函数（spec §5.3）：硬拒绝与软标记。存储相关的裁决（修订优先级、重叠冲突、
过期代次）在 facts.commit_* 里执行，这里只看批次自身。"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date

from chanapp.engine.kline import sessions
from chanapp.engine.kline.rows import (BAR_STATES, PROVENANCE_RANK, SHARES_PER_UNIT,
                                       TRADE_STATES, to_shares)

DUPLICATE_KEY = "duplicate_key"
OUT_OF_RANGE = "out_of_range"
OFF_GRID = "off_grid"
BAD_ENUM = "bad_enum"
NON_FINITE = "non_finite"
NON_POSITIVE_PRICE = "non_positive_price"
NEGATIVE_QUANTITY = "negative_quantity"
BAD_OHLC = "bad_ohlc"
FORMING_NOT_TODAY = "forming_not_today"
PREOPEN_MISSING_PC = "preopen_missing_pc"
LOWER_PROVENANCE = "lower_provenance"
FORMING_OVER_CLOSED = "forming_over_closed"
MINUTE_DAY_OHLC = "minute_day_ohlc"
VOLUME_NOT_CONSERVED = "volume_not_conserved"


@dataclass
class Verdict:
    accepted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _iso_date(text) -> bool:
    try:
        date.fromisoformat(text)
        return len(text) == 10
    except (TypeError, ValueError):
        return False


def price_problem(o, h, l, c):
    values = (o, h, l, c)
    if not all(_finite(v) for v in values):
        return NON_FINITE
    if min(values) <= 0:
        return NON_POSITIVE_PRICE
    if l > min(o, c) or h < max(o, c) or l > h:
        return BAD_OHLC
    return None


def quantity_problem(value, *, allow_none: bool):
    if value is None:
        return None if allow_none else NON_FINITE
    if not _finite(value):
        return NON_FINITE
    return NEGATIVE_QUANTITY if value < 0 else None


def _day_reason(r, counts, today, start, end):
    if counts[r.trade_date] > 1:
        return DUPLICATE_KEY
    if (not _iso_date(r.trade_date) or r.trade_date > today
            or (start and r.trade_date < start) or (end and r.trade_date > end)):
        return OUT_OF_RANGE
    if r.provenance not in PROVENANCE_RANK or r.volume_unit not in SHARES_PER_UNIT or r.sf not in (0, 1):
        return BAD_ENUM
    if r.provenance == "preopen":
        if r.trade_date != today:
            return OUT_OF_RANGE
        return None if _finite(r.pc) and r.pc > 0 else PREOPEN_MISSING_PC
    prices = (r.open, r.high, r.low, r.close)
    if r.sf == 0 or any(p is not None for p in prices):
        reason = price_problem(*prices)
        if reason:
            return reason
    reason = (quantity_problem(r.volume, allow_none=r.sf == 1)
              or quantity_problem(r.amount, allow_none=True))
    if reason:
        return reason
    if r.pc is not None and not (_finite(r.pc) and r.pc > 0):
        return NON_FINITE
    return None


def check_day_rows(rows, *, today, start=None, end=None) -> Verdict:
    verdict = Verdict()
    counts = Counter(r.trade_date for r in rows)
    for r in rows:
        reason = _day_reason(r, counts, today, start, end)
        (verdict.rejected.append((r, reason)) if reason else verdict.accepted.append(r))
    return verdict


def _minute_reason(r, counts, grid, today, start_slot, end_slot):
    if counts[r.slot_end] > 1:
        return DUPLICATE_KEY
    if (len(r.slot_end) != 16 or r.slot_end[10] != " " or r.slot_end[:10] != r.trade_date
            or not _iso_date(r.trade_date) or r.trade_date > today or (start_slot and r.slot_end < start_slot)
            or (end_slot and r.slot_end > end_slot)):
        return OUT_OF_RANGE
    if r.slot_end[11:] not in grid:
        return OFF_GRID
    if r.state not in BAR_STATES or r.trade_state not in TRADE_STATES or r.volume_unit not in SHARES_PER_UNIT:
        return BAD_ENUM
    if r.state == "forming" and r.trade_date != today:
        return FORMING_NOT_TODAY
    return (price_problem(r.open, r.high, r.low, r.close)
            or quantity_problem(r.volume, allow_none=False)
            or quantity_problem(r.amount, allow_none=True))


def check_minute_rows(rows, *, market, fact_freq, today, start_slot=None, end_slot=None) -> Verdict:
    verdict = Verdict()
    grid = set(sessions.slots(market, fact_freq))
    counts = Counter(r.slot_end for r in rows)
    for r in rows:
        reason = _minute_reason(r, counts, grid, today, start_slot, end_slot)
        (verdict.rejected.append((r, reason)) if reason else verdict.accepted.append(r))
    return verdict


def mark_trade_state(rows, prev_traded_close):
    """零成交、四价相等且等于上一根有成交 bar 收盘的行标 no_trade（spec §5.2）。"""
    out = []
    last = prev_traded_close
    for r in sorted(rows, key=lambda x: x.slot_end):
        if r.trade_state == "suspended":
            out.append(r)
            continue
        flat = r.open == r.high == r.low == r.close
        if r.volume == 0 and flat and last is not None and r.close == last:
            out.append(replace(r, trade_state="no_trade"))
            continue
        if r.volume > 0:
            last = r.close
        out.append(r)
    return out


def _get(row, key):
    return row[key] if isinstance(row, dict) or hasattr(row, "keys") else getattr(row, key)


def traded_ohlc(minute_rows):
    ordered = sorted(minute_rows, key=lambda r: _get(r, "slot_end"))
    traded = [r for r in ordered if _get(r, "volume") > 0 and _get(r, "trade_state") != "suspended"]
    if not traded:
        return None
    return (_get(traded[0], "open"), max(_get(r, "high") for r in traded),
            min(_get(r, "low") for r in traded), _get(traded[-1], "close"))


def minute_day_mismatch(minute_rows, day_row, tol: float):
    ohlc = traded_ohlc(minute_rows)
    if _get(day_row, "sf") == 1:
        return None if ohlc is None else MINUTE_DAY_OHLC
    if ohlc is None:
        return MINUTE_DAY_OHLC
    expected = tuple(_get(day_row, k) for k in ("open", "high", "low", "close"))
    return None if all(abs(a - b) <= tol for a, b in zip(ohlc, expected)) else MINUTE_DAY_OHLC


def volume_not_conserved(minute_rows, day_row, rel_tol: float = 0.001):
    day_volume = to_shares(_get(day_row, "volume"), _get(day_row, "volume_unit"))
    if not day_volume:
        return None
    total = sum(to_shares(_get(r, "volume"), _get(r, "volume_unit")) for r in minute_rows)
    return None if abs(total - day_volume) <= rel_tol * day_volume else VOLUME_NOT_CONSERVED
