"""raw provider 协议：只取数、结构检查、单位与标签归一，输出 rows.* 行；不复权、不跨源回落。"""
from __future__ import annotations

from typing import Protocol


class ProviderError(Exception):
    """取数失败（消息已脱敏）。"""


class ProviderRangeError(ProviderError):
    """返回行越出请求范围：整批作废，登记缺口。"""


class ProviderServerError(ProviderError):
    """上游 5xx：登记缺口，不当作历史终点。"""


class ProviderConnectionError(ProviderError):
    """连接类失败（超时、断连、DNS）：计入单源冷却，不消耗缺口重试次数。"""


class ProviderUnsupported(ProviderError):
    """该能力未验证或不支持（如 P3 前的指数盘中、PH1 前的港股 m5）：跳过，不计入熔断与退避。"""


class RawProvider(Protocol):
    name: str
    CONTRACT_VERSION: str

    def day_history(self, code: str, start: str, end: str) -> list: ...

    def minute_history(self, code: str, fact_freq: str, start: str, end: str, *, now) -> list: ...

    def minute_live(self, code: str, fact_freq: str, *, now) -> list: ...

    # 可选方法：来源声明了对应能力时采集器才调用（calendar、preopen_ref、instrument、qfq_series），
    # 缺方法按 ProviderUnsupported 处理。签名以 collector 的调用点为准。
