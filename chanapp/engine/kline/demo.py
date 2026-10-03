"""离线实例的本地目录、历史报价与独立计算审计；图表仍使用同一 views/计算核心。"""
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from chanapp.engine.kline import bindings, facts
from chanapp.engine.kline.rows import FetchItem, kind_of, market_of


def search(conn, query):
    query = (query or "").strip().casefold()
    if not query:
        return []
    # 导入 CSV 不要求名称。只列有事实的样本，名称优先取同库标的清单；不混入全市场目录。
    rows = conn.execute("SELECT code, COALESCE(i.name, s.code) AS name FROM "
                        "(SELECT code FROM current_day_bars UNION SELECT code FROM minute_bars) s "
                        "LEFT JOIN instruments i USING(code) ORDER BY code").fetchall()
    return [{"code": r["code"], "name": r["name"], "type": kind_of(r["code"])} for r in rows
            if query in r["code"].casefold() or query in r["name"].casefold()][:30]


def quote(conn, code):
    """实际样本末端的未复权报价，不将历史收盘伪装成昨天或今天。"""
    freq = bindings.binding(market_of(code), kind_of(code), FetchItem.MINUTE_HISTORY).minute_fact_freq
    with facts.read_txn(conn):
        days = [r for r in facts.read_day_rows(conn, code)
                if r["provenance"] == "final" and r["sf"] == 0 and r["close"] is not None]
        minutes = [r for r in facts.read_minute_rows(conn, code, freq) if r["state"] == "closed"
                   and r["trade_state"] != "suspended"] if freq else []
    day = days[-1] if days else None
    minute = minutes[-1] if minutes else None
    if day is None and minute is None:
        return None
    use_minute = minute is not None and (day is None or minute["trade_date"] > day["trade_date"])
    row = minute if use_minute else day
    pc = day["pc"] if day and day["trade_date"] == row["trade_date"] else None
    return {"price": row["close"], "price_time": row["slot_end"] if use_minute else row["trade_date"],
            "price_label": "历史", "pc": pc, "pct": round((row["close"] / pc - 1) * 100, 2) if pc else None,
            "limit_up": False, "trade_date": row["trade_date"], "stale": False}


def status(conn, codes):
    datasets = []
    for code in codes:
        freq = bindings.binding(market_of(code), kind_of(code), FetchItem.MINUTE_HISTORY).minute_fact_freq
        for dataset in filter(None, ("day", freq)):
            datasets.append({"code": code, "dataset": dataset,
                             "last_commit_at": facts.last_commit_at(conn, code, dataset),
                             "stale": False, "stale_age_s": None, "stale_judged": False,
                             "open_gaps": sum(g["reason"] != "known_gap" for g in facts.open_gaps(conn, code, dataset)),
                             "known_gaps": sum(g["reason"] == "known_gap" for g in facts.open_gaps(conn, code, dataset)),
                             "pending_review": conn.execute(
                                 "SELECT COUNT(*) FROM pending_review WHERE code=? AND dataset=? AND verdict IS NULL",
                                 (code, dataset)).fetchone()[0]})
    return {"mode": "demo", "enabled": False, "checked_at": datetime.now().isoformat(timespec="seconds"),
            "datasets": datasets, "probes": [], "budget": {}, "calendar_export": None}


def _audit_path(cache_dir) -> Path:
    return Path(cache_dir) / "demo-audit.sqlite"


def record_calc_run(cache_dir, code, freq, **values):
    # 复用现有审计 schema/事务/幂等约束；此库只记录计算，绝不作为事实读取入口。
    with closing(facts.open_facts(_audit_path(cache_dir))) as conn:
        return facts.record_calc_run(conn, code, freq, source_kind="historical_recompute", **values)


def find_calc_run(cache_dir, code, freq, input_data_version, calculation_id):
    """只读查审计库里幂等键对应的记录行；审计库尚未建立时为 None（不建库、不迁移，交给写路径）。
    每次新开只读连接：审计库被删除或替换后不会读到旧文件。"""
    path = _audit_path(cache_dir).resolve()
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return facts.find_calc_run(conn, code, freq, input_data_version, calculation_id)
