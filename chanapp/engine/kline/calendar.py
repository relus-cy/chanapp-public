"""F5 交易日历：过去（CN）由上证指数 final 日线推出，未来由供应商年表给出；港股过去由港股 final 日线推出，
年表（含未来与半日市）来自供应商 trading_days。日线推导只把该日记为开市，不覆盖供应商给的会话（半日市）；
还没有会话的过去港股日子按港交所半日市规则补上午会话（hk_scheduled_half_day）。

过去的休市工作日由相邻两个指数开市日之间的空档推出（指数自身有未解决缺口的区段除外）；
没有日历行的工作日是未知，前复权链不跨越未知日计算因子。

selfcheck 是独立 systemd timer 进程，不开事实库：采集器每日用 export_selfcheck
导出 JSON（替代手工维护的 scripts/selfcheck_calendar.json，spec §9 D13）。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from chanapp.engine.kline import facts, sessions

CN_INDEX = "sh000001"


def store_rows(conn, rows, *, source) -> None:
    for r in rows:
        existing = conn.execute("SELECT source FROM calendar WHERE market=? AND date=?",
                                (r.market, r.date)).fetchone()
        if existing and existing["source"] == "index_day":
            if r.is_open and r.sessions:     # 开市以日线为准，半日市会话仍取供应商
                conn.execute("UPDATE calendar SET sessions=? WHERE market=? AND date=?",
                             (json.dumps(list(r.sessions)), r.market, r.date))
            continue
        conn.execute("INSERT OR REPLACE INTO calendar(market, date, is_open, sessions, source,"
                     " fetched_at) VALUES (?,?,?,?,?,?)",
                     (r.market, r.date, int(r.is_open), json.dumps(list(r.sessions)), source,
                      facts.now_iso()))


def _derive(conn, market, sql, args) -> int:
    count = 0
    for row in conn.execute(sql, args).fetchall():
        conn.execute("INSERT INTO calendar(market, date, is_open, sessions, source, fetched_at)"
                     " VALUES (?,?,1,'[]','index_day',?) ON CONFLICT(market, date) DO UPDATE SET"
                     " is_open=1, source='index_day', fetched_at=excluded.fetched_at",
                     (market, row["trade_date"], facts.now_iso()))
        count += 1
    return count


def _derive_closed(conn, market, opened, code) -> int:
    """相邻两个指数开市日之间的工作日由指数连续性证明休市；指数自身在该段有未解决缺口时留作未知。"""
    gaps = [(g["start"], g["end"]) for g in facts.open_gaps(conn, code, "day")]
    count = 0
    for a, b in zip(opened, opened[1:]):
        day = date.fromisoformat(a) + timedelta(days=1)
        while day.isoformat() < b:
            iso = day.isoformat()
            if day.weekday() < 5 and not any(s <= iso <= e for s, e in gaps):
                count += conn.execute("INSERT OR IGNORE INTO calendar(market, date, is_open, sessions, source,"
                                      " fetched_at) VALUES (?,?,0,'[]','index_day',?)",
                                      (market, iso, facts.now_iso())).rowcount
            day += timedelta(days=1)
    return count


def derive_cn_past(conn) -> int:
    count = _derive(conn, "CN", "SELECT trade_date FROM current_day_bars WHERE code=?"
                    " AND provenance='final' AND sf=0", (CN_INDEX,))
    opened = [r["trade_date"] for r in conn.execute(
        "SELECT trade_date FROM current_day_bars WHERE code=? AND provenance='final' AND sf=0"
        " ORDER BY trade_date", (CN_INDEX,))]
    return count + _derive_closed(conn, "CN", opened, CN_INDEX)


# 港交所半日市：圣诞前夕、新年前夕、农历新年前夕逢工作日只开上午（前夕逢周末则当年没有半日市）。供应商年表
# trading_days 只回答最近一年（采集器每天取今年与去年），更早的半日市只能靠这条规则补会话。规则对今天以前、还没有
# 会话的全部开市日生效，不按年表回答范围截断：启动重开 known_gap 时只推导不联网，年表一年以内的半日市可能还没取到。
# 年表给出的会话与规则一致，规则之外的特殊会话仍以年表为准（不改已有会话）。农历新年日期表到 2030 年。
# 核对：事实库 2023-09-28 至 2026-10-21 全部港股都没有下午 m30 的日子恰为
# 2024-02-09、2024-12-24、2024-12-31、2025-01-28、2025-12-24、2025-12-31、2026-02-16。
_LUNAR_NEW_YEAR = ("2023-01-22", "2024-02-10", "2025-01-29", "2026-02-17", "2027-02-06", "2028-01-26",
                   "2029-02-13", "2030-02-03")
HK_HALF_DAY_SESSIONS = (sessions.SESSIONS["HK"][0],)


def hk_scheduled_half_day(day: str) -> bool:
    d = date.fromisoformat(day)
    if d.weekday() >= 5:
        return False
    eves = {f"{d.year}-12-24", f"{d.year}-12-31"} | {
        (date.fromisoformat(lny) - timedelta(days=1)).isoformat() for lny in _LUNAR_NEW_YEAR}
    return day in eves


def derive_hk_past(conn, today: date | None = None) -> int:
    count = _derive(conn, "HK", "SELECT DISTINCT trade_date FROM current_day_bars"
                    " WHERE code LIKE 'hk%' AND provenance='final' AND sf=0", ())
    for row in conn.execute("SELECT date FROM calendar WHERE market='HK' AND is_open=1 AND sessions='[]'"
                            " AND date<?", ((today or date.today()).isoformat(),)).fetchall():
        if hk_scheduled_half_day(row["date"]):
            conn.execute("UPDATE calendar SET sessions=? WHERE market='HK' AND date=?",
                         (json.dumps([list(x) for x in HK_HALF_DAY_SESSIONS]), row["date"]))
    return count


def trading_days(conn, market, start, end) -> list:
    return [r["date"] for r in conn.execute(
        "SELECT date FROM calendar WHERE market=? AND is_open=1 AND date>=? AND date<=?"
        " ORDER BY date", (market, start, end))]


def closed_days(conn, market, start, end) -> list:
    """已知休市的日期（有日历行且 is_open=0）；没有日历行的工作日是未知，不在此列。"""
    return [r["date"] for r in conn.execute(
        "SELECT date FROM calendar WHERE market=? AND is_open=0 AND date>=? AND date<=?"
        " ORDER BY date", (market, start, end))]


_BOUNDS_SQL = "SELECT MIN(date) AS lo, MAX(date) AS hi FROM calendar WHERE market=? AND source!='index_day'"


def _absent_status(bounds, day) -> bool | None:
    # 供应商年表覆盖的年份里，缺席的工作日视为未知，周末视为休市
    if bounds["lo"] and bounds["lo"][:4] <= day[:4] <= bounds["hi"][:4] and date.fromisoformat(day).weekday() >= 5:
        return False
    return None


def _session_slots(market, fact_freq, day, row) -> set:
    grid = sessions.slots(market, fact_freq)
    try:
        spans = [(a, b) for a, b in json.loads(row["sessions"])] if row else []
    except (TypeError, ValueError):
        spans = []
    if spans:
        grid = [t for t in grid if any(a < t <= b for a, b in spans)]
    return {f"{day} {t}" for t in grid}


def is_trading_day(conn, market, day) -> bool | None:
    row = conn.execute("SELECT is_open FROM calendar WHERE market=? AND date=?",
                       (market, day)).fetchone()
    if row:
        return bool(row["is_open"])
    return _absent_status(conn.execute(_BOUNDS_SQL, (market,)).fetchone(), day)


def required_slots(conn, market, fact_freq, day) -> set:
    """该日应有的分钟槽位（slot_end）：日历行带会话（半日市）时只取会话内槽位，否则取完整会话网格。
    采集器的覆盖判定与视图的完整性标记共用这一口径。"""
    row = conn.execute("SELECT sessions FROM calendar WHERE market=? AND date=?", (market, day)).fetchone()
    return _session_slots(market, fact_freq, day, row)


class Span:
    """[start, end] 内日历的一次性只读快照：一条查询取回该段日历行（缺行时再取一次年表边界），逐日判断与
    is_trading_day、required_slots 同一口径。区间计算（窗口缺口、覆盖判定）用它代替逐日查询；只服务一次计算，
    不跨请求保留（日历随采集器更新）。"""

    def __init__(self, conn, market, start, end):
        self.conn, self.market, self.start, self.end = conn, market, start[:10], end[:10]
        self._rows = {r["date"]: r for r in conn.execute(
            "SELECT date, is_open, sessions FROM calendar WHERE market=? AND date>=? AND date<=?",
            (market, self.start, self.end))}
        self._bounds = None

    def _row(self, day):
        if not self.start <= day <= self.end:
            raise ValueError(f"{day} 不在日历快照 {self.start}..{self.end} 内")
        return self._rows.get(day)

    def is_trading_day(self, day) -> bool | None:
        row = self._row(day)
        if row:
            return bool(row["is_open"])
        if self._bounds is None:
            self._bounds = self.conn.execute(_BOUNDS_SQL, (self.market,)).fetchone()
        return _absent_status(self._bounds, day)

    def required_slots(self, fact_freq, day) -> set:
        return _session_slots(self.market, fact_freq, day, self._row(day))


def covered_through(conn, market) -> str | None:
    row = conn.execute("SELECT MAX(date) AS d FROM calendar WHERE market=?", (market,)).fetchone()
    return row["d"]


def export_selfcheck(conn, path, *, year) -> None:
    out = {"year": year, "sources": {"cn": "facts.calendar", "hk": "facts.calendar"}}
    for market in ("CN", "HK"):
        closed = [r["date"][5:] for r in conn.execute(
            "SELECT date FROM calendar WHERE market=? AND is_open=0 AND date LIKE ? ORDER BY date",
            (market, f"{year}-%")) if date.fromisoformat(r["date"]).weekday() < 5]
        half = [r["date"][5:] for r in conn.execute(
            "SELECT date, sessions FROM calendar WHERE market=? AND is_open=1 AND date LIKE ?"
            " ORDER BY date", (market, f"{year}-%")) if len(json.loads(r["sessions"])) == 1]
        out[market.lower()] = {"closed": closed, "half_days": half,
                               "sessions": [list(s) for s in sessions.SESSIONS[market]]}
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    tmp.replace(path)
