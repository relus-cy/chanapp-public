"""静态来源注册：工厂与可部署能力；冷备的局部能力不冒充完整市场来源。

minute_freqs=() 可声明没有分钟数据。m60 是合法产品规则，但当前没有
完整市场来源实现它，故仍拒绝选择；不能把数学可聚合当作已实测能力。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    module: str
    factory: str
    market: str
    day_kinds: tuple[str, ...]
    minute_freqs: tuple[str, ...]
    calendar: bool = False
    preopen: bool = False
    vendor_qfq: bool = False
    credentials: tuple[str, ...] = ()
    budgeted: bool = False


# 离线公开包只分发通用核心与样本；安装清单留在 adapter 边界。
try:
    from .catalog import source_specs, BINDING_DEFAULTS
except ModuleNotFoundError as exc:
    if exc.name != __package__ + ".catalog":
        raise
    REGISTRY = {}
    BINDING_DEFAULTS = []
    for market, kinds, freq in (("CN", ("stock", "index"), "m15"), ("HK", ("stock",), "m30")):
        pairs = [(kind, item) for kind in kinds for item in ("day_history", "minute_history", "minute_live")]
        pairs += [("any", "session_calendar"), ("any", "instrument_list")]
        if market == "CN":
            pairs.append(("stock", "preopen_ref"))
        BINDING_DEFAULTS.extend((market, kind, item, "import", None, "local",
                                 freq if item.startswith("minute_") else None) for kind, item in pairs)
    BINDING_DEFAULTS = tuple(BINDING_DEFAULTS)
else:
    REGISTRY = source_specs(ProviderSpec)
