"""在线来源安装清单：注册各来源的能力与凭据名，并给出默认来源绑定。

静态来源注册：工厂与可部署能力；冷备的局部能力不冒充完整市场来源。

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


REGISTRY = {
    "mairui": ProviderSpec("chanapp.engine.kline.providers.mairui", "MairuiProvider", "CN",
                           ("stock", "index"), ("m5", "m15"), calendar=True, preopen=True,
                           credentials=("MAIRUI_LICENCE",), budgeted=True),
    "longbridge": ProviderSpec("chanapp.engine.kline.providers.longbridge", "LongbridgeProvider", "HK",
                               ("stock",), ("m30",), calendar=True, vendor_qfq=True,
                               credentials=("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET",
                                            "LONGBRIDGE_ACCESS_TOKEN")),
    "baostock": ProviderSpec("chanapp.engine.kline.providers.baostock_raw", "BaostockRawProvider", "CN",
                             ("stock",), (), calendar=True),
    "pytdx": ProviderSpec("chanapp.engine.kline.providers.pytdx_raw", "PytdxRawProvider", "CN",
                          ("index",), ("m5", "m15")),
    "yahoo": ProviderSpec("chanapp.engine.kline.providers.yahoo_raw", "YahooRawProvider", "HK",
                          ("stock",), ("m30",)),
}

BINDING_DEFAULTS = (
    ("CN", "stock", 'day_history', "mairui", "baostock", "P1"),
    ("CN", "stock", 'minute_history', "mairui", "pytdx", "P2", "m15"),
    ("CN", "stock", 'minute_live', "mairui", "pytdx", "P3", "m15"),
    ("CN", "stock", 'preopen_ref', "mairui", None, "P4"),
    ("CN", "index", 'day_history', "mairui", "pytdx", "P8"),
    ("CN", "index", 'minute_history', "mairui", "pytdx", "P8", "m15"),
    ("CN", "index", 'minute_live', "mairui", "pytdx", "P3", "m15"),
    ("CN", "any", 'session_calendar', "mairui", "baostock", "P5"),
    ("CN", "any", 'instrument_list', "mairui", "baostock", "P6"),
    ("HK", "stock", 'day_history', "longbridge", "yahoo", "PH1"),
    ("HK", "stock", 'minute_history', "longbridge", "yahoo", "PH1", "m30"),
    ("HK", "stock", 'minute_live', "longbridge", "yahoo", "PH3", "m30"),
    ("HK", "any", 'session_calendar', "longbridge", None, "PH4"),
    ("HK", "any", 'instrument_list', "longbridge", None, "PH4"),
)
