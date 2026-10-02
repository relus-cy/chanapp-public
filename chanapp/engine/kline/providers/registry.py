"""静态来源注册：工厂与可部署能力；冷备的局部能力不冒充完整市场来源。

minute_freqs=() 可声明没有分钟数据。m60 是合法产品规则，但当前没有
完整市场来源实现它，故仍拒绝选择；不能把数学可聚合当作已实测能力。
"""
from dataclasses import dataclass
from .catalog import source_specs, BINDING_DEFAULTS


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


REGISTRY = source_specs(ProviderSpec)
