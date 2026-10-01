"""F5 交易日历：过去（CN）由上证指数 final 日线推出，未来由供应商年表给出；港股过去由港股 final 日线推出，
年表（含未来与半日市）来自供应商 trading_days。日线推导只把该日记为开市，不覆盖供应商给的会话（半日市）。

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


def derive_hk_past(conn) -> int:
    return _derive(conn, "HK", "SELECT DISTINCT trade_date FROM current_day_bars"
                   " WHERE code LIKE 'hk%' AND provenance='final' AND sf=0", ())


def trading_days(conn, market, start, end) -> list:
    return [r["date"] for r in conn.execute(
        "SELECT date FROM calendar WHERE market=? AND is_open=1 AND date>=? AND date<=?"
        " ORDER BY date", (market, start, end))]


def closed_days(conn, market, start, end) -> list:
    """已知休市的日期（有日历行且 is_open=0）；没有日历行的工作日是未知，不在此列。"""
    return [r["date"] for r in conn.execute(
        "SELECT date FROM calendar WHERE market=? AND is_open=0 AND date>=? AND date<=?"
        " ORDER BY date", (market, start, end))]


def is_trading_day(conn, market, day) -> bool | None:
    row = conn.execute("SELECT is_open FROM calendar WHERE market=? AND date=?",
                       (market, day)).fetchone()
    if row:
        return bool(row["is_open"])
    bounds = conn.execute("SELECT MIN(date) AS lo, MAX(date) AS hi FROM calendar WHERE market=?"
                          " AND source!='index_day'", (market,)).fetchone()
    # 供应商年表覆盖的年份里，缺席的工作日视为未知，周末视为休市
    if bounds["lo"] and bounds["lo"][:4] <= day[:4] <= bounds["hi"][:4] and date.fromisoformat(day).weekday() >= 5:
        return False
    return None


def required_slots(conn, market, fact_freq, day) -> set:
    """该日应有的分钟槽位（slot_end）：日历行带会话（半日市）时只取会话内槽位，否则取完整会话网格。
    采集器的覆盖判定与视图的完整性标记共用这一口径。"""
    grid = sessions.slots(market, fact_freq)
    row = conn.execute("SELECT sessions FROM calendar WHERE market=? AND date=?", (market, day)).fetchone()
    try:
        spans = [(a, b) for a, b in json.loads(row["sessions"])] if row else []
    except (TypeError, ValueError):
        spans = []
    if spans:
        grid = [t for t in grid if any(a < t <= b for a, b in spans)]
    return {f"{day} {t}" for t in grid}


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
