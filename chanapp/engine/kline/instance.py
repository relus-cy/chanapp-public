"""实例覆盖：四项部署选择，启动时加载，凭据只从环境读取。"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from chanapp.engine.kline import config
from chanapp.engine.kline.providers.registry import REGISTRY

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketConfig:
    source: str
    minute_fact_freq: str | None


@dataclass(frozen=True)
class InstanceConfig:
    mode: str = "demo"
    markets: dict = field(default_factory=lambda: _default_markets())
    per_minute: int = config.DEFAULT_PER_MINUTE
    per_day: int = config.DEFAULT_PER_DAY
    instance_dir: Path | None = None


def _default_markets() -> dict:
    """产品默认取自绑定表，避免两处默认漂移。"""
    from chanapp.engine.kline import bindings
    return {b.market: MarketConfig(b.primary, b.minute_fact_freq)
            for b in bindings._DEFAULT_BINDINGS if b.item is bindings.F.MINUTE_HISTORY}


def load_instance(path=None, *, environ=None) -> InstanceConfig:
    """未指定文件为 demo；指定了却不可读/非法则拒绝。相对目录以配置文件为基准。"""
    env = os.environ if environ is None else environ
    path = path if path is not None else env.get("CHANAPP_INSTANCE_CONFIG")
    data = {}
    if path:
        path = Path(path).expanduser().resolve()
        try:
            data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError("实例配置文件不可读或不是合法 JSON") from None
    _object(data, "instance", {"mode", "markets", "quota", "instance_dir"})
    mode = data.get("mode", "demo")
    if mode not in ("demo", "real"):
        raise ValueError("mode 必须为 demo 或 real")
    markets = dict(InstanceConfig().markets)
    overrides = data.get("markets", {})
    _object(overrides, "markets", set(markets))
    for market, value in overrides.items():
        _object(value, f"markets.{market}", {"source", "minute_fact_freq"})
        markets[market] = MarketConfig(value.get("source", markets[market].source),
                                       value.get("minute_fact_freq", markets[market].minute_fact_freq))
    for market, value in markets.items():
        _validate_market(market, value, mode=mode)
    quota = data.get("quota", {})
    _object(quota, "quota", {"per_minute", "per_day"})
    limits = {key: quota.get(key, default) for key, default in (
        ("per_minute", config.DEFAULT_PER_MINUTE), ("per_day", config.DEFAULT_PER_DAY))}
    for key, value in limits.items():
        if type(value) is not int or value <= 0:
            raise ValueError(f"quota.{key} 必须为正整数")
    directory = data.get("instance_dir")
    if "instance_dir" in data:
        if not isinstance(directory, str) or not directory.strip() or "\0" in directory:
            raise ValueError("instance_dir 必须为非空路径")
        directory = Path(directory).expanduser()
        directory = (path.parent / directory).resolve() if not directory.is_absolute() else directory.resolve()
    if mode == "real":
        missing = sorted({key for value in markets.values() for key in REGISTRY[value.source].credentials
                          if not env.get(key, "").strip()})
        if missing:
            raise ValueError("真实模式缺少环境变量: " + ", ".join(missing))
    return InstanceConfig(mode, markets, limits["per_minute"], limits["per_day"], directory)


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("实例配置含重复字段")
        result[key] = value
    return result


def _object(value, name, allowed):
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须为 object")
    unknown = value.keys() - allowed
    if unknown:
        raise ValueError(f"{name} 未知字段: {', '.join(sorted(unknown))}")


_IMPORT_FREQS = {"CN": (None, "m5", "m15"), "HK": (None, "m30")}


def _validate_market(market, value, *, mode):
    # 没有安装在线 adapter 的公开包仍能校验、导入和读取本地样本；不能冒充真实来源。
    if not REGISTRY and value.source == "import" and mode == "demo":
        if value.minute_fact_freq not in _IMPORT_FREQS[market]:
            raise ValueError(f"{market}.minute_fact_freq 不支持 {value.minute_fact_freq!r}")
        return
    if not isinstance(value.source, str) or value.source not in REGISTRY:
        raise ValueError(f"{market}.source 必须为已注册来源")
    spec = REGISTRY[value.source]
    required = {"stock", "index"} if market == "CN" else {"stock"}
    if (spec.market != market or not required <= set(spec.day_kinds) or not spec.calendar
            or (market == "CN" and not spec.preopen) or (market == "HK" and not spec.vendor_qfq)):
        raise ValueError(f"{market}.source 缺少完整市场的日线、日历或复权辅助能力")
    fact = value.minute_fact_freq
    if mode == "demo":
        # demo 不调用在线来源，粒度属于本地样本：按导入可用的粒度校验，不要求所选来源具备
        # （装上别的来源不会让已有 demo 失效）
        if fact not in _IMPORT_FREQS[market]:
            raise ValueError(f"{market}.minute_fact_freq 不支持 {fact!r}")
        return
    allowed = (None, "m5", "m15", "m60") if market == "CN" else (None, "m30", "m60")
    if fact not in allowed or (fact is not None and fact not in spec.minute_freqs):
        raise ValueError(f"{market}.minute_fact_freq 不支持 {fact!r}：须为该市场允许且所选来源具备的粒度")


_current: InstanceConfig | None = None


def current() -> InstanceConfig | None:
    """应用生命周期尚未初始化时为 None，直接库调用仍可注入录制 provider。"""
    return _current


def is_demo() -> bool:
    """只认启动时已校验的模式；直接库调用保持原有注入行为。"""
    return _current is not None and _current.mode == "demo"


@contextmanager
def activate(settings: InstanceConfig, cache_dir):
    """一次应用生命周期。校验完成后、接收请求前同步绑定与粒度，退出恢复库调用环境。"""
    from chanapp.engine.kline import bindings, collector
    global _current
    previous = (_current, bindings.BINDINGS, bindings._INDEX, config.QUOTA)
    if _current is not None:
        raise RuntimeError("实例配置已生效；更改配置需重启")
    try:
        bindings.configure(settings.markets)
        config.QUOTA = {source: {"per_minute": settings.per_minute, "per_day": settings.per_day,
                                 "reserve": config.QUOTA_RESERVE} for source in {v.source for v in settings.markets.values()}
                        if source in REGISTRY and REGISTRY[source].budgeted}
        _current = settings
        from chanapp.engine import instance_paths, period_preferences
        paths = instance_paths.current(cache_dir)
        frequencies = {m: v.minute_fact_freq for m, v in settings.markets.items()}
        if is_demo():
            # demo 不碰采集器与事实库；首次预置四周期且不弹窗，已有个人选择不覆盖
            period_preferences.preset(paths, frequencies)
            with period_preferences.activate(paths, frequencies):
                yield settings
            return
        worker = collector.Collector(cache_dir)
        try:
            with worker.writer() as conn:
                bindings.sync_sources(conn)
            worker.sync_minute_fact_freq()
        except (collector.CollectorLocked, OSError, sqlite3.Error):
            # 与采集器启动一致：事实库同步失败不挡已入库数据的读取，代次推进留待下次启动
            log.warning("实例绑定未同步到事实库（下次启动再做）", exc_info=True)
        finally:
            worker.conn().close()
        with period_preferences.activate(paths, frequencies, new_instance=instance_paths.is_new_instance()) as prefs:
            shared_worker = collector.shared(cache_dir)
            previous_predicate = shared_worker.minute_enabled
            shared_worker.minute_enabled = prefs.minute_enabled
            try:
                yield settings
            finally:
                shared_worker.minute_enabled = previous_predicate
    finally:
        _current, bindings.BINDINGS, bindings._INDEX, config.QUOTA = previous
