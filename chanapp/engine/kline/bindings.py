"""来源绑定：(市场, 标的类型, 取数项) → 主源、冷备、当前生效源、代次、探针结论。

本表是运行时单一事实源（spec §5.1）；kline-sources.md 的「取数项与来源绑定」表引用它。
切换只能手动（D1：不自动跨源回落）；切换推进 binding_gen，旧代次的迟到批次拒写。
港股个股历史类绑定切到冷备时，在同一事务里冻结供应商前复权缓存（spec §7 方案 (b) 规则 6）；
切回主源不解冻，由采集器在两个周期都重取成功后随发布一起解冻。
分钟事实粒度（minute_fact_freq）改动时，采集器启动时推进对应绑定代次（Collector.sync_minute_fact_freq）；
事实库各表按 fact_freq 分开，旧粒度的行、缺口与水位只读保留，回退即改回实例粒度并重启。
安装清单给出默认绑定，本表生成产品默认（实例配置默认值由它派生）；应用启动时 configure() 按实例覆盖生成运行时绑定。
"""
from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

from chanapp.engine.kline import facts, hk_vendor_qfq
from chanapp.engine.kline.rows import FetchItem
from chanapp.engine.kline.providers.catalog import REGISTRY, BINDING_DEFAULTS

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "kline_raw"
VERDICTS = ("pending", "pass", "fail", "waived")
_SCOPE_KEYS = ("endpoint", "sample", "criteria_version")


@dataclass(frozen=True)
class SourceBinding:
    market: str
    kind: str
    item: FetchItem
    primary: str
    cold: str | None
    probe_id: str
    minute_fact_freq: str | None = None


F = FetchItem
BINDINGS = tuple(SourceBinding(market, kind, F(item), primary, cold, probe, *freq)
                 for market, kind, item, primary, cold, probe, *freq in BINDING_DEFAULTS)
_INDEX = {(b.market, b.kind, b.item): b for b in BINDINGS}
_DEFAULT_BINDINGS = BINDINGS
_VENDOR_ITEMS = (F.DAY_HISTORY, F.MINUTE_HISTORY)      # 港股供应商前复权缓存依赖的取数项


def _key_kind(item: FetchItem, kind: str) -> str:
    return "any" if item in (F.SESSION_CALENDAR, F.INSTRUMENT_LIST) else kind


def binding(market: str, kind: str, item) -> SourceBinding:
    item = FetchItem(item)
    return _INDEX[(market, _key_kind(item, kind), item)]


def configure(markets) -> None:
    """启动时从产品默认生成绑定；市场内两个分钟取数项始终同粒度。"""
    global BINDINGS, _INDEX
    BINDINGS = tuple(replace(b, primary=markets[b.market].source,
                            cold=b.cold if b.cold != markets[b.market].source else None,
                            minute_fact_freq=(markets[b.market].minute_fact_freq
                                              if b.item in (F.MINUTE_HISTORY, F.MINUTE_LIVE) else None))
                     for b in _DEFAULT_BINDINGS)
    _INDEX = {(b.market, b.kind, b.item): b for b in BINDINGS}


def sync_sources(conn) -> None:
    """实例来源变化时推进代次。未变更的实例保留运维显式激活的冷备。"""
    defaults = {(b.market, b.kind, b.item): b.primary for b in _DEFAULT_BINDINGS}
    for b in BINDINGS:
        key = f"instance_source:{b.market}:{b.kind}:{b.item.value}"
        previous = facts.setting(conn, key) or defaults[(b.market, b.kind, b.item)]
        source, _ = active(conn, b.market, b.kind, b.item)
        # switch 自带写事务；中途失败时下次启动会再推进一次代次，只多一代、不会混读
        if previous != b.primary or source not in (b.primary, b.cold):
            switch(conn, b.market, b.kind, b.item, b.primary, reason="instance source changed")
        if facts.setting(conn, key) != b.primary:
            with facts.write_txn(conn):
                facts.set_setting(conn, key, b.primary)


def active(conn, market, kind, item) -> tuple:
    b = binding(market, kind, item)
    row = conn.execute("SELECT active, binding_gen FROM binding_state WHERE market=? AND kind=?"
                       " AND item=?", (b.market, b.kind, b.item.value)).fetchone()
    return (row["active"], row["binding_gen"]) if row else (b.primary, 1)


def switch(conn, market, kind, item, to_source, *, reason) -> int:
    b = binding(market, kind, item)
    if to_source not in {b.primary, b.cold} - {None}:
        raise ValueError(f"{to_source} 不是 {b.market}/{b.kind}/{b.item.value} 的主源或冷备")
    with facts.write_txn(conn):
        gen = facts.binding_gen(conn, b.market, b.kind, b.item.value) + 1
        conn.execute(
            "INSERT INTO binding_state(market, kind, item, active, binding_gen, reason, updated_at)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(market, kind, item) DO UPDATE SET"
            " active=excluded.active, binding_gen=excluded.binding_gen, reason=excluded.reason,"
            " updated_at=excluded.updated_at",
            (b.market, b.kind, b.item.value, to_source, gen, reason, facts.now_iso()))
        if (b.market, b.kind) == ("HK", "stock") and b.item in _VENDOR_ITEMS and to_source != b.primary:
            hk_vendor_qfq.freeze_rows(conn)
    return gen


def contract_hash(source: str) -> str:
    module = importlib.import_module(REGISTRY[source].module)
    digest = hashlib.sha256(module.CONTRACT_VERSION.encode())
    directory = _FIXTURES / source
    if directory.is_dir():
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def record_probe(conn, probe_id, market, kind, item, source, verdict, scope: dict) -> None:
    if verdict not in VERDICTS:
        raise ValueError(verdict)
    missing = [k for k in _SCOPE_KEYS if k not in scope]
    if missing:
        raise ValueError(f"探针 scope 缺少 {missing}")
    b = binding(market, kind, item)
    with facts.write_txn(conn):
        conn.execute(
            "INSERT INTO probe_runs(probe_id, market, kind, item, source, verdict, scope,"
            " contract_hash, verified_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (probe_id, b.market, b.kind, b.item.value, source, verdict,
             json.dumps(scope, sort_keys=True, ensure_ascii=False), contract_hash(source),
             facts.now_iso()))


def verdict_of(conn, market, kind, item, source) -> str:
    b = binding(market, kind, item)
    row = conn.execute("SELECT verdict, contract_hash FROM probe_runs WHERE market=? AND kind=?"
                       " AND item=? AND source=? ORDER BY run_id DESC LIMIT 1",
                       (b.market, b.kind, b.item.value, source)).fetchone()
    if row is None or row["contract_hash"] != contract_hash(source):
        return "pending"
    return row["verdict"]
